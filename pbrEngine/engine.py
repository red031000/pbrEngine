'''
Created on 09.09.2015

@author: Felk
'''

import gevent
import random
import re
import logging
import dolphinWatch
import os
from functools import partial
from enum import Enum
from collections import Counter

from .eps import get_pokemon_from_data

from gevent.event import AsyncResult
from .memorymap.addresses import Locations, NestedLocations, InvalidLocation
from .memorymap.values import WiimoteButton, CursorOffsets, CursorPosMenu, CursorPosBP, GuiStateMatch, GuiMatchInputExecute, DefaultValues, LoadedBPOffsets, FieldEffects
from .guiStateDistinguisher import Distinguisher
from .states import PbrGuis, PbrStates
from .util import bytesToString, floatToIntRepr, EventHook
from .abstractions import timer, cursor, match
from .avatars import AvatarsBlue, AvatarsRed
from .activepkmn import ActivePkmn

logger = logging.getLogger("pbrEngine")

dlogger = logging.getLogger("pbrDebug")
log_path = r"C:\Users\cal\Documents\main\prog\tpp\tpp\tpp repo\logs"
log_path = os.path.join(log_path, "pbrengine.log")
log_path = os.path.abspath(log_path)
#print(log_path)
# set up the file logger
formatter = logging.Formatter(
    "[%(asctime)s] %(lineno)d\t%(message)s")  # same as default
handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=1024 * 1024 * 10,
                                               backupCount=20,
                                               encoding='utf-8')
handler.setFormatter(formatter)
dlogger.addHandler(handler)
dlogger.setLevel(logging.DEBUG)

class ActionCause(Enum):
    """Reasons for why PBREngine called the action_callback."""
    REGULAR = "regular"  # regular move selection
    FAINT = "faint"  # pokemon selection after faint
    OTHER = "other"  # other causes, like forced switch by baton pass or u-turn

class ActionError(Exception):
    pass


class PBREngine():
    def __init__(self, action_callback, host="localhost", port=6000,
                 savefile_dir="pbr_savefiles",
                 savefile_with_announcer_name="saveWithAnnouncer.state",
                 savefile_without_announcer_name="saveWithoutAnnouncer.state"):
        '''
        :param action_callback:
            Will be called when a player action needs to be determined.
            Gets called with these keyword arguments:
                <turn> Current turn.  Starts at 1.
                <side> "blue" or "red".
                <slot> Int slot of the Pokemon related to this action. Either:
                    0: If Singles, or team's first pokemon slot (upper HP bar).
                    1: Team's second pokemon slot (lower HP bar, Doubles only).
                <cause> ActionCause for this player action.
                <fails> Number of how many times the current selection failed.
                    (happens for no pp/disabled move/invalid switch for example)

            The upper layer may also invoke methods of PBREngine's match
            object for further information on available move/switch options.

            Must return a tuple (primary, target, obj), where:
            <primary> Primary action. Permits 1-char string or int. One of:
                Valid moves: a, b, c, or d.
                Valid switches: 0, 1, 2, 3, 4, or 5.
            <target> Secondary target action associated with the primary action.
                Permits 1-char string or int. Valid targets are one of:
                1: other team's first pokemon (upper HP bar)
                2: other team's second pokemon (lower HP bar)
                0: self
                -1: ally
                None: target must be None in Singles battles, or when switching.
            <obj> is any object. <obj> will be submitted as an argument to
            either the on_switch or on_attack callback if this command succeeds.
        :param host: ip of the dolphin instance to connect to
        :param port: port of the dolphin instance to connect to
        :param savefile_dir: directory location of savestates
        :param savefile_with_announcer_name: filename of savefile with the announcer turned on
        :param savefile_without_announcer_name: filename of savefile with the announcer turned off
        ''' 
        self._action_callback = action_callback
        self._distinguisher = Distinguisher(self._distinguishGui)
        self._dolphin = dolphinWatch.DolphinConnection(host, port)
        self._dolphin.onDisconnect(self._reconnect)
        self._dolphin.onConnect(self._initDolphinWatch)

        os.makedirs(os.path.abspath(savefile_dir), exist_ok=True)
        self._savefile1 = os.path.abspath(os.path.join(savefile_dir, savefile_with_announcer_name))
        self._savefile2 = os.path.abspath(os.path.join(savefile_dir, savefile_without_announcer_name))

        self.timer = timer.Timer()
        self.cursor = cursor.Cursor(self._dolphin)
        self.match = match.Match(self.timer)
        self.match.on_win += self._matchOver
        self.match.on_switch += self._switched
        self.match_speed = 1.0  # animation speed during match
        # event callbacks
        '''
        Event of the winner being determined.
        Can be considered end of the match.
        arg0: <winner> "blue" "red" "draw"
        '''
        self.on_win = EventHook(winner=str)
        '''
        Event for state changes.
        Propably only useful for the debug monitor, not for production.
        arg0: <state> see states.PbrStates
        '''
        self.on_state = EventHook(state=PbrStates)
        '''
        Event of a gui changing.
        Propably only useful for the debug monitor, not for production.
        arg0: <gui> see states.PbrGuis
        '''
        self.on_gui = EventHook(gui=PbrGuis)
        '''
        Event of a pokemon attacking.
        arg0: <side> "blue" "red"
        arg1: <slot> team index of the pokemon attacking.
        arg2: <moveindex> 0-3, index of move used.
              CAUTION: <mon> might not have a move with that index (e.g. Ditto)
        arg3: <movename> name of the move used.
              CAUTION: <mon> might not have this attack (e.g. Ditto, Metronome)
        arg4: <obj> object originally returned by the action-callback that lead
              to this event. None if the callback wasn't called (e.g. Rollout)
        '''
        self.on_attack = EventHook(side=str, slot=int, moveindex=int,
                                  movename=str, obj=object)
        '''
        Event of a pokemon dying.
        arg0: <side> "blue" "red"
        arg2: <slot> team index of the dead pokemon
        '''
        self.on_death = EventHook(side=str, slot=int)
        self.match.on_death += lambda side, slot: self.on_death(
            side=side, slot=slot)
        '''
        Event of a pokemon getting sent out.
        arg0: <side> "blue" "red"
        arg1: <old_slot> team index of the pokemon called back.
        arg2: <new_slot> team index of the pokemon now fighting.
        arg3: <obj> object originally returned by the action-callback that lead
              to this event. None if the callback wasn't called (e.g. death)
        '''
        self.on_switch = EventHook(side=str, old_slot=int,
                                   new_slot=int, obj=object)
        '''
        Event of information text appearing in one of those black boxes.
        Also includes fly-by texts (It's super/not very effective!, A critical hit!)
        Includes moves failing, pokemon dying, weather effect reminders etc.
        arg0: <text> Text in the box.
        '''
        self.on_infobox = EventHook(text=str)
        '''
        Event of some stats getting updated.
        arg0: <type> what stat type got updated (e.g. "hp")
        arg1: <data> dictionary containing information on the new stat
        Examples:
        hp: {"hp": 123, "side": "blue", "slot": 0}
        pp: {"pp": 13, "side": "red", "slot": 1}  # pp currently not supported :(
        status: {"status": "brn/par/frz/slp/psn/tox", "side": "blue", "slot": 
        "2"}
            if status is "slp", the field "rounds" (remaining slp) will be included too
        '''
        self.on_stat_update = EventHook(type=str, data=dict)

        self._increasedSpeed = 20.0
        self._lastInputFrame = 0
        self._lastInput = 0
        self.volume = 0
        self.speed = 1.0
        self.state = PbrStates.INIT
        self.colosseum = 0
        self.avatar_blue = AvatarsBlue.BLUE
        self.avatar_red = AvatarsRed.RED
        self._prev_avatar_blue = AvatarsBlue.BLUE
        self._prev_avatar_red = AvatarsRed.RED
        self.announcer = True
        self.hide_gui = False
        self.gui = PbrGuis.MENU_MAIN  # most recent/last gui, for info
        self._starting_weather = None
        self.reset()

        # stuck checker
        gevent.spawn(self._stuckChecker)

    def connect(self):
        '''
        Connects do Dolphin with dolphinWatch. Should be called when the
        initialization (setting listeners etc.) is done.
        '''
        self._dolphin.connect()

    def disconnect(self):
        '''
        Disconnects from Dolphin.
        connect() needs to be called to make this instance work again.
        '''
        self._dolphin.disconnect()

    def _watcher2(self, data):
        logger.debug("{}: {:02x}".format(Locations.WHICH_MOVE.name, data))
    def _watcher3(self, data):
        logger.debug("{}: {:02x}".format(Locations.WHICH_PKMN.name, data))

    def _distinguishMatch(self, data):
        logger.debug("{}: {:08x}".format(Locations.GUI_STATE_MATCH.name, data))
        gui_type = data >> 16 & 0xff
        pkmn_input_type = data & 0xff

        if self.state == PbrStates.MATCH_RUNNING:
            # True if in the pkmn select menu, and a pkmn input needs to be made
            # (i.e., there are no popups, and a pkmn input has not successfully gone
            # through yet).
            self._fNeedPkmnInput = gui_type == GuiStateMatch.PKMN

            self._fLastGuiWasSwitchPopup = (self._guiStateMatch ==
                                            GuiStateMatch.SWITCH_POPUP)

            # True when the pkmn menu pops up, and remains true until it is gone.
            # Remains true through any "can't switch" popups.
            self._fGuiPkmnUp = pkmn_input_type in (GuiStateMatch.TARGET,
                                                   GuiStateMatch.SWITCH)
        else:
            self._fNeedPkmnInput = False
            self._fLastGuiWasSwitchPopup = False
            self._fGuiPkmnUp = False

        self._guiStateMatch = data

        # Run self._distinguishGui() with the gui.
        self._distinguisher.distinguishMatch(gui_type)

    def _initDolphinWatch(self, watcher):
        self._dolphin.volume(0)

        # ## subscribing to all indicators of interest. mostly gui
        # misc. stuff processed here
        self._subscribe(Locations.CURRENT_TURN.value, self._distinguishTurn)
        self._subscribe(Locations.CURRENT_SIDE.value, self._distinguishSide)
        self._subscribe(Locations.CURRENT_SLOT.value, self._distinguishSlot)
        self._subscribe(Locations.ORDER_LOCK_BLUE.value,            self._distinguishOrderLock)
        self._subscribe(Locations.ORDER_LOCK_RED.value,             self._distinguishOrderLock)
        self._subscribeMulti(Locations.ATTACK_TEXT.value,           self._distinguishAttack)
        self._subscribeMulti(Locations.INFO_TEXT.value,             self._distinguishInfo)
        self._subscribe(Locations.HP_BLUE.value,                    partial(self._distinguishHp, side="blue"))
        self._subscribe(Locations.HP_RED.value,                     partial(self._distinguishHp, side="red"))
        self._subscribe(Locations.STATUS_BLUE.value,                partial(self._distinguishStatus, side="blue"))
        self._subscribe(Locations.STATUS_RED.value,                 partial(self._distinguishStatus, side="red"))
        self._subscribeMulti(Locations.PNAME_BLUE.value,  partial(self._distinguishName, side="blue", slot=0))
        self._subscribeMulti(Locations.PNAME_BLUE2.value, partial(self._distinguishName, side="blue", slot=1))
        self._subscribeMulti(Locations.PNAME_RED.value,   partial(self._distinguishName, side="red", slot=0))
        self._subscribeMulti(Locations.PNAME_RED2.value,  partial(self._distinguishName, side="red", slot=1))
        self._subscribeMultiList(9, Locations.EFFECTIVE_TEXT.value, self._distinguishEffective)
        # de-multiplexing all these into single PbrGuis-enum using distinguisher
        self._subscribe(Locations.GUI_STATE_MATCH.value, self._distinguishMatch)
        self._subscribe(Locations.GUI_STATE_BP.value,           self._distinguisher.distinguishBp)
        self._subscribe(Locations.GUI_STATE_MENU.value,         self._distinguisher.distinguishMenu)
        self._subscribe(Locations.GUI_STATE_RULES.value,        self._distinguisher.distinguishRules)
        self._subscribe(Locations.GUI_STATE_ORDER.value,        self._distinguisher.distinguishOrder)
        self._subscribe(Locations.GUI_STATE_BP_SELECTION.value, self._distinguisher.distinguishBpSelect)
        self._subscribeMulti(Locations.GUI_TEMPTEXT.value,      self._distinguisher.distinguishStart)
        self._subscribe(Locations.POPUP_BOX.value,              self._distinguisher.distinguishPopup)

        self._subscribe(Locations.WHICH_MOVE.value, self._watcher2)
        self._subscribe(Locations.WHICH_PKMN.value, self._watcher3)

        # stuff processed by abstractions
        self._subscribe(Locations.CURSOR_POS.value, self.cursor.updateCursorPos)
        self._subscribe(Locations.FRAMECOUNT.value, self.timer.updateFramecount)
        # ##

        gevent.sleep(1.0) # the pause below is failing, so try waiting a bit
        # initially paused, because in state WAITING_FOR_NEW
        self._dolphin.pause()
        self._setState(PbrStates.WAITING_FOR_NEW)
        self._lastInput = WiimoteButton.TWO  # to be able to click through the menu

    def _subscribe(self, loc, callback):
        self._dolphin._subscribe(loc.length*8, loc.addr, callback)

    def _subscribeMulti(self, loc, callback):
        self._dolphin._subscribeMulti(loc.length, loc.addr, callback)

    def _subscribeMultiList(self, length, loc, callback):
        # used for a list/deque of strings
        for i in range(length):
            self._dolphin._subscribeMulti(loc.length, loc.addr+loc.length*i,
                                          callback)

    def _reconnect(self, watcher, reason):
        if (reason == dolphinWatch.DisconnectReason.CONNECTION_CLOSED_BY_HOST):
            # don't reconnect if we closed the connection on purpose
            return
        logger.warning("DolphinConnection connection closed, reconnecting...")
        if (reason == dolphinWatch.DisconnectReason.CONNECTION_FAILED):
            # just tried to establish a connection, give it a break
            gevent.sleep(3)
        self.connect()

    def reset(self):
        # Used during matches.
        self._turn = 0  # Match turns, see self._distinguishTurn.
        self._side = "blue"  # team of the Pokemon currently active.
        self._slot = 0  # team index of the Pokemon currently active.
        self._guiStateMatch = 0
        self._fLastGuiWasSwitchPopup = False
        self._fNeedPkmnInput = False
        self._fGuiPkmnUp = False

        self._selecting_moves = True
        self._next_pkmn = -1
        self._move_select_state = None
        self._numMoveSelections = 0
        self.startsignal = False
        self._fMatchCancelled = False
        self._fDoubles = False
        self._move_select_followup = None
        self._fFirstMoveSelectionOfMatch = True
        self.active = {"blue": [None] * 2, "red": [None] * 2}
        # Move selection: expect REGULAR, set next to OTHER
        # Fainted: set next to FAINTED.
        self._expectedActionCause = {"blue": [ActionCause.OTHER] * 2,
                                     "red": [ActionCause.OTHER] * 2}
        self._actionCallbackObjStore = {"blue": [None] * 2, "red": [None] * 2}

        # Used during match setup.
        self._moveBlueUsed = 0  # FIXME: never changed
        self._moveRedUsed = 0   # FIXME: never changed
        self._bp_offset = 0
        self._posBlues = []  # Used during BP selection
        self._posReds = []
        self._fSelectedSingleBattle = False
        self._fSelectedTppRules = False
        self._fBlueSelectedBP = False
        self._fBlueChoseOrder = False
        self._fEnteredBp = False
        self._fClearedBp = False
        self._fGuiPkmnUp = False
        self._fSetAnnouncer = False  # True if announcer has been set properly for this savestate?
        self._fSkipWaitForNew = False
        self._fBpPage2 = False


    ####################################################
    # The below functions are presented to the outside #
    #         Use these to control the PBR API         #
    ####################################################

    def start(self):
        '''
        Starts a prepared match.
        If the selection is not finished for some reason
        (state != WAITING_FOR_START), it will continue to prepare normally and
        start the match once it's ready.
        Otherwise calling start() will start the match by resuming the game.
        '''
        logger.debug("Starting a prepared match. startsignal: {}, state: {}"
                    .format(self.startsignal, self.state))
        self.startsignal = True
        if self.state == PbrStates.WAITING_FOR_START:
            self._initOrderSelection()

    def new(self, colosseum, pkmn_blue, pkmn_red, avatar_blue=AvatarsBlue.BLUE,
            avatar_red=AvatarsRed.RED, announcer=True, starting_weather=None,
            doubles=False):
        '''
        Starts to prepare a new match.
        If we are not waiting for a new match-setup to be initiated
        (state != WAITING_FOR_NEW), it will load the savestate anyway.
        If that fails, it will try to start preparing as soon as possible.
        CAUTION: issues a cancel() call first if the preparation reached
                 the "point of no return".
        :param colosseum: colosseum enum, choose from pbrEngine.Colosseums
        :param pkmn_blue: array with dictionaries of team blue's pokemon
        :param pkmn_red: array with dictionaries of team red's pokemon
        CAUTION: Currently only max. 3 pokemon per team supported.
        :param avatar_blue=AvatarsBlue.BLUE: enum for team blue's avatar
        :param avatar_red=AvatarsRed.RED: enum for team red's avatar
        :param announcer=True: boolean if announcer's voice is enabled
        '''
        logger.debug("Preparing a new match. startsignal: {}, state: {}"
                    .format(self.startsignal, self.state))
        self.reset()
        if self.state >= PbrStates.PREPARING_START and \
           self.state <= PbrStates.MATCH_RUNNING:
            # TODO this doesn't work after startup!
            logger.warning("Detected invalid match state: {}.  Cancelling match"
                           .format(self.state))
            self.cancel()
            self._fSkipWaitForNew = True
        for _ in range(25):
            if self.state != PbrStates.MATCH_ENDED:
                break
            logger.warning("PBR is not yet ready for a new match")
            gevent.sleep(1) # Wait until self._waitForNew completes.  At that time,
                            # PBR has finished all actions from the previous match
        self.colosseum = colosseum
        self._fDoubles = doubles = True
        self._posBlues = list(range(len(pkmn_blue)))
        self._posReds = list(range(len(pkmn_blue), len(pkmn_blue)+len(pkmn_red)))
        self.match.new(pkmn_blue, pkmn_red, doubles)
        self.avatar_blue = avatar_blue
        self.avatar_red = avatar_red
        self.announcer = announcer
        self._starting_weather = starting_weather

        # try to load savestate
        # if that succeeds, skip a few steps
        self._setState(PbrStates.EMPTYING_BP2)
        self._dolphin.resume()
        loaded_success = self._dolphin.load(self._savefile1 if announcer
                                            else self._savefile2)
        # wait until loaded, just to be sure
        self.timer.sleep(80)
        if not loaded_success:
            self._setState(PbrStates.CREATING_SAVE1)
        else:
            self._setAnimSpeed(self._increasedSpeed)

        self._newRng()  # avoid patterns
        self._dolphin.volume(100)

    def cancel(self):
        '''
        Cancels the current/upcoming match at the next move selection menu.
        Does nothing if the match is already over.
        CAUTION: A match will be ended by giving up at the next possibility,
        but the result will be reported as "draw"!
        '''
        self._fMatchCancelled = True

    def setVolume(self, v):
        '''
        Sets the game's volume during matches.
        Will always be 0 during selection, regardless of this setting.
        :param v: integer between 0 and 100.
        '''
        self.volume = v
        self._dolphin.volume(v)

    def setSpeed(self, s):
        '''
        Sets the game's emulation speed.
        :param s: emulation speed as a float, with 1.0 being normal speed, 0.5 being half speed, etc.
        '''
        self.speed = s
        self._dolphin.speed(s)

    def setFov(self, val=0.5):
        '''
        Sets the game's field of view.
        :param val=0.5: float, apparently in radians, 0.5 is default
        '''
        self._dolphin.write32(Locations.FOV.value.addr, floatToIntRepr(val))

    def setFieldEffectStrength(self, val=1.0):
        '''
        Sets the animation strength of the game's field effects (weather, etc).
        :param val: animation strength as a float
        '''
        self._dolphin.write32(Locations.FIELD_EFFECT_STRENGTH.value.addr, floatToIntRepr(val))

    def setGuiPosY(self, val=DefaultValues["GUI_POS_Y"]):
        '''
        Sets the Gui's y-coordinate.
        :param val=DefaultValues["GUI_POS_Y"]: integer, y-coordinate of gui
        '''
        self._dolphin.write32(Locations.GUI_POS_Y.value.addr, floatToIntRepr(val))

    #######################################################
    #             Below are helper functions.             #
    # They are just bundling or abstracting functionality #
    #######################################################

    def _disableBlur(self):
        '''
        Disables the weird multirender-blur-thingy.
        '''
        self._dolphin.write32(Locations.BLUR1.value.addr, 0xffffffff)
        self._dolphin.write32(Locations.BLUR2.value.addr, 0xffffffff)

    def _resetBlur(self):
        '''
        Resets the blur-values to their original.
        This is necessary, because these values are used for something else
        during selection!
        '''
        self._dolphin.write32(Locations.BLUR1.value.addr, DefaultValues["BLUR1"])
        self._dolphin.write32(Locations.BLUR2.value.addr, DefaultValues["BLUR2"])

    def _setAnimSpeed(self, val):
        '''
        Sets the game's animation speed.
        Does not influence frame-based "animations" like text box speeds.
        Does not influence loading times.
        Is automatically increased during selection as a speed improvement.
        :param v: float describing speed
        '''
        if val == 1.0:
            self._resetAnimSpeed()
        else:
            self._dolphin.write32(Locations.SPEED_1.value.addr, 0)
            self._dolphin.write32(Locations.SPEED_2.value.addr, floatToIntRepr(val))

    def _resetAnimSpeed(self):
        '''
        Sets the game's animation speed back to its default.
        '''
        self._dolphin.write32(Locations.SPEED_1.value.addr, DefaultValues["SPEED1"])
        self._dolphin.write32(Locations.SPEED_2.value.addr, DefaultValues["SPEED2"])

    def _switched(self, side, old_slot, new_slot):
        self.on_switch(side=side, old_slot=old_slot, new_slot=new_slot,
                      obj=self._actionCallbackObjStore[side][new_slot])
        self._actionCallbackObjStore[side][new_slot] = None

    def _stuckChecker(self):
        '''
        Shall be spawned as a Greenlet.
        Checks if no input was performed within the last 5 i2fMap seconds.
        If so, it assumes the last input got lost and repeats that.
        '''
        while True:
            self.timer.sleep(20)
            # stuck limit: 5 seconds. No stuckchecker during match.
            if self.state == PbrStates.MATCH_RUNNING:
                continue
            limit = 300
            if self.state in (PbrStates.CREATING_SAVE1,
                              PbrStates.CREATING_SAVE2)\
                    and self.gui not in (PbrGuis.MENU_MAIN,
                                         PbrGuis.MENU_BATTLE_PASS,
                                         PbrGuis.BPS_SELECT):
                limit = 80
            if self.gui == PbrGuis.RULES_BPS_CONFIRM:
                limit = 600  # don't interrupt the injection
            if (self.timer.frame - self._lastInputFrame) > limit:
                dlogger.warning("Stuck checker will press %s" % WiimoteButton(self._lastInput).name)
                self._pressButton(self._lastInput)

    def _selectLater(self, frames, index):
        self._setLastInputFrame(frames)
        self.timer.spawn_later(frames, self._select, index)

    def _pressLater(self, frames, button):
        self._setLastInputFrame(frames)
        self.timer.spawn_later(frames, self._pressButton, button)

    def _setLastInputFrame(self, framesFromNow):
        '''Manually account for a button press that
        will occur in the future after some number of frames.
        Helps prevent a trigger happy stuckchecker.'''
        self._lastInputFrame = self.timer.frame + framesFromNow

    def _pressButton(self, button):
        '''Propagates the button press to dolphinWatch.
        Often used, therefore bundled'''
        self._lastInputFrame = self.timer.frame
        self._lastInput = button
        if button != 0:
            dlogger.info("> " + WiimoteButton(button).name)
        self._dolphin.wiiButton(0, button)

    def _select(self, index):
        '''Changes the cursor position and presses Two.
        Often used, therefore bundled.'''
        self.cursor.setPos(index)
        dlogger.info("Cursor set to {}, will press {}"
                     .format(index, WiimoteButton.TWO.name))
        self._pressButton(WiimoteButton.TWO)

    def _pressTwo(self):
        '''Presses Two. Often used, therefore bundled.'''
        self._pressButton(WiimoteButton.TWO)

    def _pressOne(self):
        '''Presses One. Often used, therefore bundled.'''
        self._pressButton(WiimoteButton.ONE)

    def _setState(self, state):
        '''
        Sets the current PBR state. Fires the on_state event if it changed.
        Always use this method to change the state, or events will be missed.
        '''
        if self.state == state:
            return
        self.state = state
        dlogger.info("[State] " + PbrStates(state).name)
        self.on_state(state=state)

    def _newRng(self):
        '''Helper method to replace the RNG-seed with a random 32 bit value.'''
        self._dolphin.write32(Locations.RNG_SEED.value.addr, random.getrandbits(32))

    def _read32(self, addr, **kwargs):
        return self._read(32, addr, **kwargs)

    def _read16(self, addr, **kwargs):
        return self._read(16, addr, **kwargs)

    def _read8(self, addr, **kwargs):
        return self._read(8, addr, **kwargs)

    def _read(self, mode, addr, most_common_of=5):
        '''Read <mode> bytes at the given address

        Returns most commonly read value of <most_common_of> reads to reduce
        likelihood of faulty reads (usually reading 0 instead of the correct value).
        '''
        if mode not in [8, 16, 32]:
            raise ValueError("Mode must be 8, 16, or 32, got {}".format(mode))
        values = Counter()
        for _ in range(most_common_of):
            val = AsyncResult()
            self._dolphin.read(mode, addr, val.set)
            val = val.get()
            values[val] += 1
        return values.most_common(1)[0][0]

    def _write32(self, addr, val, **kwargs):
        return self._write(32, addr, val, **kwargs)

    def _write16(self, addr, val, **kwargs):
        return self._write(16, addr, val, **kwargs)

    def _write8(self, addr, val, **kwargs):
        return self._write(8, addr, val, **kwargs)

    def _write(self, mode, addr, val, max_attempts=5,
               writes_per_attempt=5, reads_per_attempt=5):
        '''Write <mode> bytes of val to the given address

        Perform up to <max_attempts> write-and-verify attempts.
        '''
        newVal = None
        if mode not in [8, 16, 32]:
            raise ValueError("Mode must be 8, 16, or 32, got {}".format(mode))
        for i in range(max_attempts):
            for _ in range(writes_per_attempt):
                self._dolphin.write(mode, addr, val)
            newVal = self._read(mode, addr, most_common_of=reads_per_attempt)
            if newVal == val:
                break
            else:
                logger.warning("Write verification failed attempt {}/{}. Read {}, expected {}"
                               .format(i, max_attempts, newVal, val))
        if not newVal == val:
            logger.error("Write of {} to {:0X} failed".format(val, addr))
        return newVal == val

    def _checkNestedLocs(self):
        while True:
            self.timer.sleep(20)
            try:
                fieldEffectsLoc = NestedLocations.FIELD_EFFECTS.value.getAddr(self._read)
                fieldEffects = self._read32(fieldEffectsLoc, most_common_of=5)
                dlogger.info("Field effects at {:08X} has value {:08X}"
                             .format(fieldEffectsLoc, fieldEffects))
            except InvalidLocation:
                dlogger.error("Failed to determine starting weather location")

            try:
                ibBlueLoc = NestedLocations.IB_BLUE.value.getAddr(self._read)
                ibBlue = self._read16(ibBlueLoc, most_common_of=5)
                dlogger.info("Blue species at {:08X} has value {:08X}"
                             .format(ibBlueLoc, ibBlue))
            except InvalidLocation:
                dlogger.error("Failed to determine starting ib-blue location")

    def _firstMoveSelectionOfMatch(self):
        if self._starting_weather:
            self._setStartingWeather()
        self._setupInBattlePkmn()
        self._fFirstMoveSelectionOfMatch = False


    def temp_callback(self, side, slot, name, val):
        if self.state != PbrStates.MATCH_RUNNING:
            return
        logger.info("{} {}: {} is now {:0X}".format(side, slot, name, val))


    def _setupInBattlePkmn(self):
        activeLoc = NestedLocations.ACTIVE_PKMN.value.getAddr(self._read)
        if activeLoc == -1:
            logger.error("Failed to determine location of active pkmn")
            return

        offset = 0
        for slot in (0, 1):
            for side in ("blue", "red"):
                callback = partial(self.temp_callback, side, slot)
                if slot == 1 and not self._fDoubles:
                    active = None
                else:
                    # PBR forces doubles battles to start with >=2 mons per side.
                    active = ActivePkmn(side, slot, activeLoc + offset,
                                        self._dolphin, callback)
                offset += NestedLocations.ACTIVE_PKMN.value.length
                self.active[side][slot] = active
                print("Created IB pkmn: {} {} {}".format(side, slot, active))


    def _setStartingWeather(self):
        '''Set weather before the first turn of the battle

        When this sets the starting weather, the animation for the weather that
        is set will not appear until the end of turn 1, despite being in play at
        the start of turn 1.

        Does not set starting weather if weather already exists at move selection time,
        eg. Drought causing sun.

        Non-weather field effects such as Gravity, etc. are not supported by this function
        (and their animations wouldn't work anyway)
        '''
        fieldEffectsLoc = NestedLocations.FIELD_EFFECTS.value.getAddr(self._read)
        if fieldEffectsLoc == -1:
            logger.error("Failed to determine starting weather location")
            return
        fieldEffects = self._read32(fieldEffectsLoc, most_common_of=10)
        logger.debug("Field effects at {:08X} has value {:08X}"
                     .format(fieldEffectsLoc, fieldEffects))
        weather = fieldEffects & FieldEffects.WEATHER_MASK
        if weather == 0:  # Only overwrite weather related bits
            newFieldEffects = self._starting_weather | fieldEffects
            logger.debug("Writing field effects: {:08X} to address {:08X}"
                         .format(newFieldEffects, fieldEffectsLoc))
            self._write32(fieldEffectsLoc, newFieldEffects)

    def _injectPokemon(self):
        bp_groups_loc = NestedLocations.LOADED_BPASSES_GROUPS.value.getAddr(self._read)
        if bp_groups_loc == -1:
            logger.error("Failed to determine bp structs location")
            return

        for side_offset, data in ((LoadedBPOffsets.BP_BLUE, self.match.pkmn["blue"]),
                                  (LoadedBPOffsets.BP_RED, self.match.pkmn["red"])):
            pkmnLoc = bp_groups_loc + LoadedBPOffsets.GROUP2 + side_offset + LoadedBPOffsets.PKMN
            for poke_i, pkmn_dict in enumerate(data):
                pokemon = get_pokemon_from_data(pkmn_dict)
                pokebytes = pokemon.to_bytes()
                self._dolphin.pause()
                gevent.sleep(0.1)
                for i, byte in enumerate(pokebytes):
                    self._dolphin.write8(pkmnLoc + i + poke_i*0x8c, byte)
                gevent.sleep(0.1)
                self._dolphin.resume()
                self.timer.sleep(20)

    def pkmnSlotToButton(self, slot):
        # TODO fix sideways remote
        return [
            WiimoteButton.RIGHT,    # 1st Pokemon, onscreen up
            WiimoteButton.DOWN,     # 2nd "" right
            WiimoteButton.UP,       # 3rd "" left
            WiimoteButton.LEFT,     # 4th "" down
            WiimoteButton.TWO,      # 5th "" two
            WiimoteButton.ONE       # 6th "" one
        ][slot]

    def _getInputState(self):
        return (self._turn, self._side, self._slot, self._numMoveSelections)

    ############################################
    # The below functions are for timed inputs #
    #        or processing "raw events"        #
    ############################################

    def _confirmPkmn(self):
        '''
        Clicks on the confirmation button on a pokemon selection screen
        for battle passes. Shall be called/spawned as a cursorevent after a
        pokemon has been selected for a battlepass.
        Must have that delay because the pokemon model has to load.
        Adds the next cursorevent for getting back to the battle pass slot view
        '''
        self._pressTwo()
        self._bp_offset += 1
        if self.state == PbrStates.PREPARING_BP1:
            self._posBlues.pop(0)
        else:
            self._posReds.pop(0)
        cursor = CursorOffsets.BP_SLOTS - 1 + self._bp_offset
        self.cursor.addEvent(cursor, self._distinguishBpSlots)

    def _initOrderSelection(self):
        '''
        Done once for each team.
        Simply presses the right wiimote button, which transitions
        the gui state from PbrGuis.ORDER_SELECT to PbrGuis.ORDER_CONFIRM.
        '''
        self._dolphin.resume()
        self._setState(PbrStates.SELECTING_ORDER)
        self._pressButton(WiimoteButton.RIGHT)

    def _initMatch(self):
        '''
        Is called when a match start is initiated.
        '''
        self._setAnimSpeed(self.match_speed)
        # mute the "whoosh" as well
        self.timer.spawn_later(330, self._dolphin.volume, 100)
        self.timer.spawn_later(450, self._disableBlur)
        # match is running now
        self._setState(PbrStates.MATCH_RUNNING)

    def _matchOver(self, winner):
        '''
        Is called when the current match ended and a winner is determined.
        Sets the cursorevent for when the "Continue/Change Rules/Quit"
        options appear.
        Calls the on_win-callback and triggers a matchlog-message.
        '''
        if self.state != PbrStates.MATCH_RUNNING:
            return
        self._fMatchCancelled = False  # reset flag here
        self.cursor.addEvent(1, self._quitMatch)
        self._setState(PbrStates.MATCH_ENDED)
        self.on_win(winner=winner)

    def _waitForNew(self):
        if not self._fSkipWaitForNew:
            self._dolphin.pause()
            self._setState(PbrStates.WAITING_FOR_NEW)
        else:
            self._setState(PbrStates.CREATING_SAVE1)
            self._fSkipWaitForNew = False  # redundant?

    def _quitMatch(self):
        '''
        Is called as a cursorevent when the "Continue/Change Rules/Quit"
        options appear.
        Clicks on "Quit" and resets the PBR engine into the next state.
        Next state can either be waiting for a new match selection (pause),
        or directly starting one.
        '''
        self._dolphin.volume(0)
        self._resetBlur()
        self._select(3)
        self._setAnimSpeed(self._increasedSpeed)
        # make sure this input gets processed before a potential savestate-load
        self.timer.spawn_later(30, self._waitForNew)

    def _nextPkmn(self):
        '''
        Is called when the pokemon selection menu pops up- a switch
        selection in singles, and a switch or target selection in doubles.

        Worker exits only upon one of:
        - a successful selection
        - a return to the move select menu
        - detection of a different state (happens when menu times out)
        '''
        logger.debug("Entered _nextPkmn. State: {}".format(self._getInputState()))
        # The coming loop sleeps, so use recorded_state to ensure we exit if
        # the move selection timer hit zero.
        if self._move_select_followup:   # Here from the move select menu
            from_move_select = True
            recorded_state, next_pkmn, is_switch = self._move_select_followup
            self._move_select_followup = None  # reset
        else:   # Here from faint / baton pass / etc.
            from_move_select = False
            recorded_state = self._getInputState()
            next_pkmn = None
            is_switch = True  # Can't be a target, so must be a switch.

        # shift gui back to normal position
        if self.hide_gui:
            self.setGuiPosY(100000.0)
        else:
            self.setGuiPosY(DefaultValues["GUI_POS_Y"])

        # The action callback might sleep.  Spawn a worker so self._distinguishGui()
        # doesn't get delayed as well.
        gevent.spawn(self._nextPkmnWorker, from_move_select, recorded_state,
                     next_pkmn, is_switch)

    def _nextPkmnWorker(self, from_move_select, recorded_state,
                        next_pkmn, is_switch):
        if not from_move_select:
            _, next_pkmn = self._getAction(True)

        # silent = not is_switch  # Only beep when switching.
        silent = True

        iterations = 0
        while self._fGuiPkmnUp and recorded_state == self._getInputState():
            logger.debug("nextPkmn iteration {}. State: {}"
                         .format(iterations, recorded_state))
            if iterations < 4 and self._fNeedPkmnInput:
                # Press appropriate button.
                # Both silent and real presses may need a few iterations to work.
                if silent:
                    if is_switch:
                        self._dolphin.write32(Locations.INPUT_EXECUTE.value.addr,
                                              GuiMatchInputExecute.EXECUTE_SWITCH)
                        self._dolphin.write8(Locations.INPUT_EXECUTE2.value.addr,
                                              GuiMatchInputExecute.EXECUTE_SWITCH2)
                        button_index = next_pkmn  # onscreen up == 0, right == 1, etc.
                    else:  # Targeting. Must be doubles
                        self._dolphin.write32(Locations.INPUT_EXECUTE.value.addr,
                                              GuiMatchInputExecute.EXECUTE_TARGET)
                        # Silent values don't correspond to the onscreen button values
                        if self._side == "blue":
                            button_index = [1,2,4,8][next_pkmn]
                        else:
                            button_index = [2,1,8,4][next_pkmn]
                    self._dolphin.write8(Locations.WHICH_PKMN.value.addr,
                                         button_index)
                    logger.info("> {} (silent, pokemon select)".format(
                        self.pkmnSlotToButton(next_pkmn).name))
                else:
                    button = self.pkmnSlotToButton(next_pkmn)
                    self._pressButton(button)
            elif iterations >= 4:
                # Selection is taking too long- assume a popup has appeared and
                # we cannot switch. Should only be possible if we entered the
                # switch menu from the move select menu (arena trap, etc).
                if not from_move_select:
                    logger.error("Incorrectly assumed popup; now stuck in nextPkmn")
                # Alternate between pressing "2" and "Minus" to get back to the
                # move selection.
                if iterations % 2:  # Click away popup.
                    self._pressTwo()
                else:  # Click to go back to move select.
                    self._pressButton(WiimoteButton.MINUS)
            iterations += 1
            logger.debug("nextPkmn loop sleeping...")
            self.timer.sleep(20)
        logger.debug("Exiting nextPkmn. Current state: {}".format(self._getInputState()))

    def _getRandomAction(self, moves=True, switch=True):
        actions = []
        if moves:
            actions += ["a", "b", "c", "d"]
        elif switch:
            actions += [1, 2, 3, 4, 5, 6]
        return random.choice(actions)

    def _getAction(self, switch_only):
        '''Get action from action callback. Returns either of these tuples:
        ("move", <next_move>, <next_pkmn>), or ("switch", <next_pkmn>).
        '''
        # Transform to conventional turn count, indexed at 1.
        turn = self._turn + 2 - int(bool(switch_only)) - 1
        side = self._side
        slot = self._slot
        # Currently we allow all moves to be selected.
        moves = [] if switch_only else ["a", "b", "c", "d"]
        # Disallow selecting Pokemon not present: crash risk.
        switches = self.match.getSwitchOptions(side)
        # TODO: disallow selecting Pokemon not present: crash risk
        targets = None if switch_only else [[1, 2, 0, -1] for _ in moves]
        # `cause` will get ActionCause.OTHER, unless _nextMove() just set it to
        # ActionCause.REGULAR, or a detected faint set it to ActionCause.FAINT.
        cause = self._expectedActionCause[side][slot]
        self._expectedActionCause[side][slot] = ActionCause.OTHER

        # retrieve action
        primary, target, obj = self._action_callback(
            turn=turn, side=side, slot=slot, cause=cause,
            fails=self._numMoveSelections)
        self._actionCallbackObjStore[self._side][self._slot] = obj

        # Convert actions to int where possible.
        primary = str(primary).lower()
        if primary in ("0", "1", "2", "3", "4", "5"):
            primary = int(primary)
        if target in ("-1", "0", "1", "2"):
            target = int(target)

        if primary in moves:
            next_move = ord(primary.lower()) - ord('a')
            if self._fDoubles:
                logger.debug("Side: {} target: {} slot: {}"
                            .format(side, target, slot))

                # determine target side index & target slot
                if target in (1, 2):  # foe team
                    target_side_index = int(side == "blue")
                    target_slot = target - 1
                elif target in (0, -1):  # self team
                    target_side_index = int(side == "red")
                    if target == 0:  # self
                        target_slot = slot
                    else:  # ally
                        target_slot = 1 - slot
                else:
                    raise ActionError("Forbidden doubles target: %r " % target)
                next_pkmn = target_side_index + 2 * target_slot
                
            else:
                if target is not None:
                    logger.error("Target must be None in Singles, was %r",
                                 target)
                next_pkmn = -1  # Indicates no next pokemon
            logger.debug("received action: {}".format(("move", next_move, next_pkmn)))
            return ("move", next_move, next_pkmn)
        elif primary in switches:
            next_pkmn = int(primary)
            logger.debug("received action: {}".format(("switch", next_pkmn)))
            return ("switch", next_pkmn)
        else:
            raise ActionError("Invalid player action: %r "
                              "with moves: %s and switches: %s" %
                              (primary, moves, switches))

    def _nextMove(self):
        '''
        Is called once the move selection screen pops up.
        Triggers the action-callback that prompts the upper layer to
        decide for a move/switch.

        Sort of a misnomer as it can also select to enter the switch or draw menus.
        '''
        self._selecting_moves = True
        recorded_state = self._getInputState()
        logger.debug("Entered nextMove. State: {}".format(recorded_state))
        # The action callback might sleep.  Spawn a worker so self._distinguishGui()
        # doesn't get delayed as well.
        gevent.spawn(self._nextMoveWorker, recorded_state)

    def _nextMoveWorker(self, recorded_state):
        if self._fFirstMoveSelectionOfMatch:
            self._firstMoveSelectionOfMatch()
        # prevent "Connection with wiimote lost bla bla"
        self._pressButton(WiimoteButton.NONE)  # no button press

        if self._fMatchCancelled:  # quit the match if it was cancelled
            self._dolphin.write32(Locations.INPUT_EXECUTE.value.addr,
                                  GuiMatchInputExecute.INSTA_GIVE_IN)
            self._matchOver("draw")
            return
        self._expectedActionCause[self._side][self._slot] = ActionCause.REGULAR
        action = self._getAction(False)  # May sleep
        if recorded_state != self._getInputState():
            logger.warning("Aborting nextMove due to input state expiration. "
                           "Recorded state: {} Current state: {}"
                           .format(recorded_state, self._getInputState()))
            return

        # Execute the move or switch.
        self._numMoveSelections += 1
        recorded_state = self._getInputState()  # Get incremented numMoveSelections

        # silent = action[0] == "move"  # Only beep when switching.
        silent = True

        if action[0] == "move":
            next_move, next_pkmn = action[1], action[2]
            if silent:
                logger.info("> {} (silent)".format(self.pkmnSlotToButton(
                    next_move).name))
                # this hides and locks the gui until a move was inputted.
                self._dolphin.write32(Locations.INPUT_EXECUTE.value.addr,
                                      GuiMatchInputExecute.EXECUTE_MOVE)
                self._dolphin.write8(Locations.INPUT_EXECUTE2.value.addr,
                                     GuiMatchInputExecute.EXECUTE_MOVE2)
                self._dolphin.write8(Locations.WHICH_MOVE.value.addr, next_move)
            else:
                button = self.pkmnSlotToButton(next_move)
                self._pressButton(button)
            if self._fDoubles:
                self._move_select_followup = (recorded_state, next_pkmn, False)
            # In doubles, the Pokemon select menu will popup now.
        elif action[0] == "switch":
            next_pkmn = action[1]
            self._move_select_followup = (recorded_state, next_pkmn, True)
            if silent:
                logger.info("> TWO (silent move selection)")
                self._dolphin.write32(Locations.INPUT_EXECUTE.value.addr,
                                      GuiMatchInputExecute.EXECUTE_SWITCH_MENU)
            else:
                self._pressTwo()
        else:  # should only be "move" or "switch"
            assert False
        logger.debug("Exiting nextMove. Current state: {}".format(recorded_state))

    def _skipIntro(self):
        '''
        Started as a gevent job after the battle passes are confirmed.
        Start spamming 2 to skip the intro before the order selection.
        '''
        while self.gui == PbrGuis.RULES_BPS_CONFIRM:
            self._pressTwo()
            self.timer.sleep(20)

    def _invalidateEffTexts(self):
        for i in range(9):
            self._dolphin.write32(Locations.EFFECTIVE_TEXT.value.addr +
                                  Locations.EFFECTIVE_TEXT.value.length * i,
                                  0x00230023)

    def _select_bp(self, num):
        index = CursorOffsets.BPS + (num % 4)
        if not self._fBpPage2 and num >= 4:
            self._select(CursorPosBP.BP_NEXT)
            self._fBpPage2 = True
            self._selectLater(60, index)
        elif self._fBpPage2 and num < 4:
            self._select(CursorPosBP.BP_PREV)
            self._fBpPage2 = False
            self._selectLater(60, index)
        else:
            self._select(index)

    ##############################################
    # Below are callbacks for the subscriptions. #
    #   It's really ugly, I know, don't judge.   #
    #   Their job is to know what to do when a   #
    #          certain value changes.            #
    ##############################################

    def _distinguishTurn(self, val):
        # See Locations.CURRENT_TURN
        if self.state != PbrStates.MATCH_RUNNING:
            return
        if val != self._turn + 1:
            raise ValueError("Detected val {}, expected {} (last val + 1)"
                             .format(val, self._turn + 1))
        self._turn += 1
        self._selecting_moves = False
        logger.debug("New turn detected: %d" % self._turn)
        self._cleanupAfterTurn()

    def _distinguishSide(self, val):
        # See Locations.CURRENT_SIDE
        if self.state != PbrStates.MATCH_RUNNING:
            return
        if not val in (0, 1):
            raise ValueError("Invalid side detected: %d" % val)
        self._side = "blue" if val == 0 else "red"
        logger.debug("New side detected: %s" % self._side)
        self._cleanupAfterMove()

    def _distinguishSlot(self, val):
        # See Locations.CURRENT_SLOT
        if self.state != PbrStates.MATCH_RUNNING:
            return
        if not val in (0, 1):
            raise ValueError("Invalid side detected: %d" % val)
        self._slot = val
        logger.debug("New slot detected: %d" % self._slot)
        self._cleanupAfterMove()

    def _cleanupAfterTurn(self):
        # An entire turn (all move selections and their associated pkmn selections)
        # has completed.

        # Reset these.  The game's move shot clock may hit zero after _nextMove() and
        # before _nextPkmn(), in which case these must be reverted to correct
        # values.
        logger.debug("Resetting move select followup and expected action causes")
        self._move_select_followup = None
        self._expectedActionCause = {"blue": [ActionCause.OTHER] * 2,
                                     "red": [ActionCause.OTHER] * 2}
        self._cleanupAfterMove()

    def _cleanupAfterMove(self):
        # A move selection (and its associated pkmn selection if any) has completed.
        # Cleanup any leftover state (may be needed if the move timed out due to
        # PBR's move "shot clock".
        logger.debug("Resetting fails count")
        self._numMoveSelections = 0  # reset fails counter

    def _distinguishName(self, data, side, slot):
        if self.state != PbrStates.MATCH_RUNNING:
            return
        assert 0 <= slot and slot <= 1
        if not self._fDoubles and slot == 1:
            return  # The second pokemon isn't in battle during singles.
        name = bytesToString(data)
        self.match.newInBattleName(side, slot, name)

    def _distinguishHp(self, val, side):
        return
        # if val == 0 or self.state != PbrStates.MATCH_RUNNING:
        #     return
        # self.on_stat_update(type="hp", data={"hp": val, "side": side,
        #                                      "slot": ???})

    def _distinguishStatus(self, val, side):
        # status = {
        #     0x00: None,
        #     0x08: "psn",
        #     0x10: "brn",
        #     0x20: "frz",
        #     0x40: "par",
        #     0x80: "tox"  # badly poisoned
        # }.get(val, "slp")  # slp can be 0x01-0x07
        # if status == "slp":
        #     # include rounds remaining on sleep
        #     self.on_stat_update(type="status", data={"status": status, "side": side, "rounds": val,
        #                                              "slot": current_slot})
        # else:
        #     self.on_stat_update(type="status", data={"status": status, "side": side,
        #                                              "slot": current_slot})
        return

    def _distinguishEffective(self, data):
        # Just for the logging. Can also be "critical hit"
        if self.state != PbrStates.MATCH_RUNNING:
            return
        # move gui back into place. Don't hide this even with hide_gui set
        self.setGuiPosY(DefaultValues["GUI_POS_Y"])
        text = bytesToString(data)
        # skip text invalidations
        if text.startswith("##"):
            return
        self.on_infobox(text=text)
        # this text gets instantly changed, so change it after it's gone.
        # this number of frames is a wild guess.
        # Longer than "A critical hit! It's super effective!"
        self.timer.spawn_later(240, self._invalidateEffTexts)

    def _distinguishAttack(self, data):
        # Gets called each time the attack-text
        # (Team XYZ's pkmn used move) changes

        # Ignore these data changes when not in a match
        if self.state != PbrStates.MATCH_RUNNING:
            return

        # 2nd line starts 0x40 bytes later and contains the move name only
        line = bytesToString(data[:0x40]).strip()
        # convert, then remove "!"
        move = bytesToString(data[0x40:]).strip()[:-1]

        match = re.search(r"^Team (Blue|Red)'s (.*?) use(d)", line)
        if match:
            # invalidate the little info boxes here.
            # I think there will always be an attack declared between 2
            # identical texts ("But it failed" for example)
            # => No need for timed invalidation
            self._dolphin.write32(Locations.INFO_TEXT.value.addr, 0x00230023)

            # "used" => "uses", so we get the event again if something changes!
            self._dolphin.write8(Locations.ATTACK_TEXT.value.addr + 1 +
                                 2 * match.start(3), 0x73)
            side = match.group(1).lower()
            slot = self.match.getSlotByName(side, match.group(2))
            self.match.setLastMove(side, move)
            # reset fails counter
            self._numMoveSelections = 0
            self.on_attack(side=side, slot=slot, moveindex=0,
                           movename=move,
                           obj=self._actionCallbackObjStore[side][slot])
            self._actionCallbackObjStore[side][slot] = None

    def _distinguishInfo(self, data):
        # Gets called each time the text in the infobox (xyz fainted, abc hurt
        # itself, etc.) changes and gets analyzed for possible events of
        # interest.

        # Ignore these data changes when not in a match
        if self.state != PbrStates.MATCH_RUNNING:
            return

        string = bytesToString(data)

        # skip text invalidation
        if string.startswith("##"):
            return

        # shift gui up a bit to fully see this
        self.setGuiPosY(DefaultValues["GUI_POS_Y"] + 20.0)

        # log the whole thing
        self.on_infobox(text=string)

        # CASE 1: Someone fainted.
        match = re.search(r"^Team (Blue|Red)'s (.+?) fainted!$",
                          string)
        if match:
            side = match.group(1).lower()
            self.match.getSlotByName(side, match.group(2))
            self.match.fainted(side, match.group(2))
            self._expectedActionCause[side][self._slot] = ActionCause.FAINT
            return

        # CASE 2: Roar or Whirlwind caused a undetected pokemon switch!
        match = re.search(
            r"^Team (Blue|Red)'s (.+?) was dragged out!$", string)
        if match:
            side = match.group(1).lower()
            self.match.draggedOut(side, match.group(2))
            self.match.getSlotByName(side, match.group(2))
            return

        # update the win detection for each (unprocessed) message.
        # e.g. "xyz was buffeted by the sandstorm" takes extra time for
        # the 2nd pokemon to die and therefore needs a timer reset
        self.match.update_winning_checker()

    def _distinguishOrderLock(self, val):
        # This value becomes 1 if at least 1 pokemon has been selected for
        # order. for both sides.
        # Enables the gui to lock the order in. Bring up that gui by pressing 1
        if val == 1:
            self._pressOne()

    def _distinguishBpSlots(self):
        # Decide what to do if we are looking at a battle pass...
        # Chronologically: clear #2, clear #1, fill #1, fill #2
        if self.state <= PbrStates.EMPTYING_BP2:
            # We are still in the state of clearing the 2nd battle pass
            if self._fClearedBp:
                # There are no pokemon on this battle pass left
                # Go back and start emptying battle pass #1
                self._pressOne()
                self._setState(PbrStates.EMPTYING_BP1)
            else:
                # There are still pokemon on the battle pass. Grab that.
                # Triggers gui BPS_PKMN_GRABBED
                self._select(CursorOffsets.BP_SLOTS)
        elif self.state == PbrStates.EMPTYING_BP1:
            # There are still old pokemon on blue's battle pass. Grab that.
            # Triggers gui BPS_PKMN_GRABBED
            if self._fClearedBp:
                self._fClearedBp = False
                self._setState(self.state + 1)
                self._pressOne()
            else:
                self._select(CursorOffsets.BP_SLOTS)
        elif self.state <= PbrStates.PREPARING_BP2:
            # We are in the state of preparing the battlepasses
            if (self.state == PbrStates.PREPARING_BP1 and not self._posBlues)\
                    or (self.state == PbrStates.PREPARING_BP2 and not
                        self._posReds):
                # if the current battle pass has been filled with all pokemon:
                # enter next state and go back
                self._setState(self.state + 1)
                self._pressOne()
            else:
                # The old pokemon have been cleared, click on last slot (#6) to
                # start filling the slots
                self._select(CursorOffsets.BP_SLOTS + 5)

    def _distinguishBpsSelect(self):
        self._bp_offset = 0
        self._fEnteredBp = False
        if self.state in (PbrStates.CREATING_SAVE1, PbrStates.CREATING_SAVE2)\
                and self._fSetAnnouncer:
            self._resetAnimSpeed()
            # wait for game to stabilize. maybe this causes the load fails.
            gevent.sleep(0.5)
            # announcer=True, state=save1:  savefile2
            # announcer=True, state=save2:  savefile1
            # announcer=False, state=save1: savefile1
            # announcer=False, state=save2: savefile2
            self._dolphin.save(self._savefile1 if self.announcer !=
                               (self.state == PbrStates.CREATING_SAVE1)
                               else self._savefile2)
            gevent.sleep(1.0)  # I don't think this caused the saves to go corrupt, but better be save
            self._setAnimSpeed(self._increasedSpeed)
            self._fSetAnnouncer = False
            self._setState(self.state + 1)

        if self.state == PbrStates.EMPTYING_BP2:
            self._fClearedBp = False
            self._select_bp(self._prev_avatar_red)
        elif self.state == PbrStates.EMPTYING_BP1:
            self._fClearedBp = False
            self._select_bp(self._prev_avatar_blue)
        elif self.state == PbrStates.PREPARING_BP1:
            self._select_bp(self.avatar_blue)
        elif self.state == PbrStates.PREPARING_BP2:
            self._prev_avatar_blue = self.avatar_blue
            self._prev_avatar_red = self.avatar_red
            self._select_bp(self.avatar_red)
        else:
            # done preparing or starting to prepare savestates
            self._pressOne()

    def _distinguishGui(self, gui):
        # Might be None if the guiStateDistinguisher didn't recognize the value.
        if not gui:
            return

        # BIG switch statement incoming :(
        # what to do on each screen

        # Assign gui to self.gui as the switch may use self.gui for some comparisons.
        # If no if-elif picks this gui up, revert self.gui and return without
        # triggering the on_gui event.
        # Question: Why can't any gui be picked up safely?
        # Answer: Some values trigger random guis while in a completely different
        # state (such as MATCH_POPUP outside of battle). Those guis need rejecting
        # to avoid disruptions (like a `while self.gui == <some value>` that
        # shouldn't be disrupted because self.gui got assigned a garbage value).
        backup = self.gui
        self.gui = gui

        try:
            dlogger.debug("[Gui] {}  ({})".format(
                PbrGuis(gui).name, PbrStates(self.state).name))
            if gui == backup:
                # Expected with some guis, such as RULES_SETTINGS.
                dlogger.warning("[Duplicate Gui] {}  ({})".format(
                    PbrGuis(gui).name, PbrStates(self.state).name))
        except:  # unrecognized gui, ignore
            dlogger.error("Unrecognized gui or state: {} / {}"
                          .format(gui, self.state))
        # MAIN MENU
        if gui == PbrGuis.MENU_MAIN:
            if not self._fSetAnnouncer and self.state in\
                    (PbrStates.CREATING_SAVE1, PbrStates.CREATING_SAVE2):
                self._select(CursorPosMenu.SAVE)
            elif self.state < PbrStates.PREPARING_STAGE:
                self._select(CursorPosMenu.BP)
            else:
                self._select(CursorPosMenu.BATTLE)
                # hack correct stuff as "default"
                # seems to not work? Not doing this anymore
                # self._dolphin.write32(Locations.DEFAULT_BATTLE_STYLE.value.addr,
                # BattleStyles.SINGLE)
                # self._fSelectedSingleBattle = True
                # self._dolphin.write32(Locations.DEFAULT_RULESET.value.addr,
                # Rulesets.RULE_1)
                # self._fSelectedTppRules = True
        elif gui == PbrGuis.MENU_BATTLE_TYPE:
            if self.state < PbrStates.PREPARING_STAGE:
                self._pressOne()
            else:
                self._select(2)
        elif gui == PbrGuis.MENU_BATTLE_PASS:
            if self.state >= PbrStates.PREPARING_STAGE or \
                    (not self._fSetAnnouncer and self.state in
                     (PbrStates.CREATING_SAVE1, PbrStates.CREATING_SAVE2)):
                self._pressOne()
            else:
                self._select(1)
                self._fBpPage2 = False
            self._setAnimSpeed(self._increasedSpeed)

        elif gui == PbrGuis.MENU_BATTLE_PLAYERS:
            if self.state < PbrStates.PREPARING_STAGE:
                self._pressOne()
            else:
                self._select(2)
        elif gui == PbrGuis.MENU_BATTLE_REMOTES:
            if self.state < PbrStates.PREPARING_STAGE:
                self._pressOne()
            else:
                self._select(1)
        elif gui == PbrGuis.MENU_SAVE:
            self._select(1)
        elif gui == PbrGuis.MENU_SAVE_CONFIRM:
            self._select(CursorPosMenu.SAVE_CONFIRM + 1)  # don't save
        elif gui == PbrGuis.MENU_SAVE_CONTINUE:
            self._select(2)  # no, quit please
            # slow down because of intro
        elif gui == PbrGuis.MENU_SAVE_TYP2:
            # handled with timed event
            self._pressLater(60, WiimoteButton.TWO)
            # TODO: ferraro: what is this?
            self.timer.spawn_later(120, self._resetAnimSpeed)  # to not get stuck in the demo
            self.timer.spawn_later(600, self._resetAnimSpeed)  # to not get stuck in the demo

        # START MENU
        elif gui == PbrGuis.START_MENU:
            if not self._fSetAnnouncer and self.state in\
                    (PbrStates.CREATING_SAVE1, PbrStates.CREATING_SAVE2):
                self._selectLater(10, 3)  # options
            else:
                self._selectLater(10, 1)  # colosseum mode
        elif gui == PbrGuis.START_OPTIONS:
            # announcer=True, state=save1:  set flag to 0
            # announcer=True, state=save2:  set flag to 1
            # announcer=False, state=save1: set flag to 1
            # announcer=False, state=save2: set flag to 0
            if self.announcer != (self.state == PbrStates.CREATING_SAVE1):
                self._dolphin.write8(Locations.ANNOUNCER_FLAG.value.addr, 1)
            elif self.announcer != (self.state == PbrStates.CREATING_SAVE2):
                self._dolphin.write8(Locations.ANNOUNCER_FLAG.value.addr, 0)
            self._pressLater(10, WiimoteButton.ONE)
            self._fSetAnnouncer = True

        elif gui in (PbrGuis.START_OPTIONS_SAVE, PbrGuis.START_MODE,
                     PbrGuis.START_SAVEFILE, PbrGuis.START_WIIMOTE_INFO):
            # START_SAVEFILE is not working,
            # but I am relying on the unstucker anyway...
            self._setAnimSpeed(self._increasedSpeed)
            self._pressLater(10, WiimoteButton.TWO)

        # BATTLE PASS MENU
        elif gui == PbrGuis.BPS_SELECT and\
                self.state < PbrStates.PREPARING_START:
            # done via cursorevents
            self.cursor.addEvent(CursorOffsets.BPS, self._distinguishBpsSelect)
        elif gui == PbrGuis.BPS_SLOTS and\
                self.state < PbrStates.PREPARING_START:
            if self.state < PbrStates.CREATING_SAVE2:
                # accidentially entered BP
                self._pressOne()
            elif not self._fEnteredBp:
                self._distinguishBpSlots()
        elif gui == PbrGuis.BPS_PKMN_GRABBED:
            self._select(CursorPosBP.REMOVE)
        elif gui == PbrGuis.BPS_BOXES and\
                self.state < PbrStates.PREPARING_START:
            self._fEnteredBp = True
            self._fClearedBp = True
            #if self.state == PbrStates.EMPTYING_BP1:
            #    self._setState(PbrStates.PREPARING_BP1)
                # no need to go back to bp selection first, short-circuit
            if self.state == PbrStates.PREPARING_BP1:
                self._select(CursorOffsets.BOX + (self._posBlues[0] // 30))
            elif self.state == PbrStates.PREPARING_BP2:
                self._select(CursorOffsets.BOX + (self._posReds[0] // 30))
            else:
                self._pressOne()
                self.cursor.addEvent(CursorOffsets.BP_SLOTS,
                                     self._distinguishBpSlots)
        elif gui == PbrGuis.BPS_PKMN and\
                self.state < PbrStates.PREPARING_START:
            if self.state == PbrStates.PREPARING_BP1:
                self._select(CursorOffsets.PKMN + (self._posBlues[0] % 30))
            else:
                self._select(CursorOffsets.PKMN + (self._posReds[0] % 30))
            self.cursor.addEvent(1, self._confirmPkmn)
        elif gui == PbrGuis.BPS_PKMN_CONFIRM and\
                self.state < PbrStates.PREPARING_START:
            # handled with cursorevent, because the model loading has
            # a delay and therefore breaks the indicator
            pass

        # RULES MENU (stage, settings etc, but not battle pass selection)
        elif gui == PbrGuis.RULES_STAGE:
            if self.state < PbrStates.PREPARING_STAGE:
                self._pressOne()
            else:
                self._dolphin.write32(Locations.COLOSSEUM.value.addr, self.colosseum)
                self._select(CursorOffsets.STAGE)
                self._setState(PbrStates.PREPARING_START)
        elif gui == PbrGuis.RULES_SETTINGS:
            if not self._fSelectedTppRules:
                # cursorevents
                self.cursor.addEvent(CursorOffsets.RULESETS, self._select,
                                     False, CursorOffsets.RULESETS+1)
                self.cursor.addEvent(CursorPosMenu.RULES_CONFIRM,
                                     self._pressTwo)
                self._select(1)
                self._fSelectedTppRules = True
            elif not self._fDoubles and not self._fSelectedSingleBattle:
                self._select(2)
                self._fSelectedSingleBattle = True
            else:
                # this is always the case since the default-hacks
                self._select(3)
                self._fSelectedSingleBattle = False
                self._fSelectedTppRules = False
        elif gui == PbrGuis.RULES_BATTLE_STYLE:
            self._select(1)
        elif gui == PbrGuis.RULES_BPS_CONFIRM:
            # twice, just to be sure as I have seen it fail once
            self._injectPokemon()
            self._injectPokemon()
            self._pressTwo()
            # skip the followup match intro
            gevent.spawn_later(1, self._skipIntro)

        # BATTLE PASS SELECTION
        # (chronologically before PbrGuis.RULES_BPS_CONFIRM)
        # overlaps with previous battle pass menu. Therefore the state checks
        # TODO improve that, maybe cluster it together?
        elif gui == PbrGuis.BPSELECT_SELECT and\
                self.state >= PbrStates.PREPARING_START:
            self._fBpPage2 = False
            if self._fBlueSelectedBP:
                self.cursor.addEvent(CursorOffsets.BPS, self._select_bp, True,
                                     self.avatar_red)
                self._fBlueSelectedBP = False
            else:
                self.cursor.addEvent(CursorOffsets.BPS, self._select_bp, True,
                                     self.avatar_blue)
                self._fBlueSelectedBP = True
        elif gui == PbrGuis.BPSELECT_CONFIRM and\
                self.state >= PbrStates.PREPARING_START:
            self._pressTwo()

        # PKMN ORDER SELECTION
        elif gui == PbrGuis.ORDER_SELECT:
            logger.debug("ORDER_SELECT. startsignal: {}, state: {}"
                           .format(self.startsignal, self.state))

            if self.startsignal:
                # start() was called.  Match needs to start, so
                # initiate order selection.
                self._initOrderSelection()
            else:
                # Wait for start() to initiate order selection.
                self._setState(PbrStates.WAITING_FOR_START)
                self._dolphin.pause()

            # TODO fix sideways remote

        elif gui == PbrGuis.ORDER_CONFIRM:
            logger.debug("ORDER_CONFIRM")
            def orderToInts(order):
                vals = [0x07]*6
                for i, v in enumerate(order):
                    vals[v-1] = i+1
                # y u no explain, past me?
                return (vals[0] << 24 | vals[1] << 16 | vals[2] << 8 | vals[3],
                        vals[4] << 8 | vals[5])
            if self._fBlueChoseOrder:
                self._fBlueChoseOrder = False
                x1, x2 = orderToInts(list(range(1, 1+len(self.match.pkmn["red"]))))
                self._dolphin.write32(Locations.ORDER_RED.value.addr, x1)
                self._dolphin.write16(Locations.ORDER_RED.value.addr+4, x2)
                self._pressTwo()
                self._initMatch()
            else:
                self._fBlueChoseOrder = True
                x1, x2 = orderToInts(list(range(1, 1+len(self.match.pkmn["blue"]))))
                self._dolphin.write32(Locations.ORDER_BLUE.value.addr, x1)
                self._dolphin.write16(Locations.ORDER_BLUE.value.addr+4, x2)
                self._pressTwo()

        # GUIS DURING A MATCH, mostly delegating to safeguarded loops and jobs
        elif gui == PbrGuis.MATCH_FADE_IN:
            # try early: shift gui back to normal position
            if self.hide_gui:
                self.setGuiPosY(100000.0)
            else:
                self.setGuiPosY(DefaultValues["GUI_POS_Y"])
        elif gui == PbrGuis.MATCH_MOVE_SELECT:
            # we can safely assume we are in match state now
            self._setState(PbrStates.MATCH_RUNNING)
            # shift gui back to normal position
            if self.hide_gui:
                self.setGuiPosY(100000.0)
            else:
                self.setGuiPosY(DefaultValues["GUI_POS_Y"])
            # erase the "xyz used move" string, so we get the event of it
            # changing.
            # Change the character "R" or "B" to 0, so this change won't get
            # processed.
            self._dolphin.write8(Locations.ATTACK_TEXT.value.addr + 11, 0)
            # overwrite RNG seed
            self._newRng()
            # start the job that handles the complicated and dangerous process
            # of move selection
            self._nextMove()
        elif gui == PbrGuis.MATCH_PKMN_SELECT:
            # In switching, fires redundantly after a "can't switch" popup disappears.
            # In targeting, there are no popups, so redundant fires do not occur.

            # If there is a move select followup, the call is not redundant, because
            # _nextPkmn() consumes followups immediately.
            if self._move_select_followup:  # REGULAR ActionCause
                self._nextPkmn()
            elif (not self._selecting_moves and  # FAINT / OTHER ActionCause
                    not self._fLastGuiWasSwitchPopup):  # And not redundant
                self._nextPkmn()
        elif gui == PbrGuis.MATCH_IDLE:
            pass  # Accept this gui for possible on_gui event logging.
        elif gui == PbrGuis.MATCH_POPUP and\
                self.state == PbrStates.MATCH_RUNNING:
            # This gui only fires on invalid move selection popups.
            self._pressTwo()

        else:
            self.gui = backup  # Reject the gui change.
            try:
                dlogger.debug("[Gui Rejected] {}  ({})".format(
                    PbrGuis(gui).name, PbrStates(self.state).name))
            except:
                dlogger.error("Unrecognized gui or state: {} / {}"
                              .format(gui, self.state))
            return  # Don't trigger the on_gui event.

        # Trigger the on_gui event now.
        # The gui is considered valid if we reach here.
        self.on_gui(gui=gui)
