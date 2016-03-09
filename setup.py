
from setuptools import setup, find_packages
setup(
    name="pbrEngine",
    version="0.1",
    packages=find_packages(),
    install_requires=['dolphinWatch'],

    author="Felk",
    description="Library based on DolphinWatch to offer automation of Pokemon Battle Revolution matches for TwitchPlaysPokemon.",
    url="https://github.com/TwitchPlaysPokemon/pbrEngine",
)