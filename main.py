import argparse
import logging
import os
import requests
import concurrent.futures

from typing import Literal

####################################################################################################
# Argument Parser and Logging Setup

RED     = "\033[91m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
ORANGE  = "\033[38;5;208m"
RESET   = "\033[0m"


parser = argparse.ArgumentParser()
group = parser.add_mutually_exclusive_group(required=True)
group.add_argument("-n", "--name", help="The name of the player", type=str)
group.add_argument("-l", "--list", help="Directory to a namelist", type=str)
parser.add_argument("-v", "--verbose", help="Enable verbose logging", action="store_true")
parser.add_argument("-o", "--output", help="Output the available results to a file (does not save -n)", type=str)
args = parser.parse_args()


class LevelFormatter(logging.Formatter):
    def __init__(self, formats, datefmt=None):
        super().__init__(datefmt=datefmt)
        self.formats = formats

    def format(self, record):
        # pick the right format depending on log level
        log_fmt = self.formats.get(record.levelno, self.formats[logging.INFO])
        formatter = logging.Formatter(log_fmt, datefmt=self.datefmt)
        return formatter.format(record)

formats = {
    logging.INFO: "%(asctime)s %(levelname)s\t%(message)s",
    logging.ERROR: "%(asctime)s \033[91m%(levelname)s\033[0m\t%(message)s\033[0m",
    logging.WARNING: "%(asctime)s \033[93m%(levelname)s\033[0m\t%(message)s\033[0m"
}

handler = logging.StreamHandler()
handler.setFormatter(LevelFormatter(formats, datefmt="%Y-%m-%d %H:%M:%S"))

logger = logging.getLogger()
# Set log level based on verbose flag - only show INFO and above unless verbose is enabled
logger.setLevel(logging.DEBUG if args.verbose else logging.INFO)
logger.addHandler(handler)

####################################################################################################


class NameFinder:

    URL = "https://api.mojang.com/users/profiles/minecraft/{}"

    LEGAL_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789_"

    AVAIALBLE = 1
    UNAVAILABLE = 0
    UNKNOWN = -1
    ILLEGAL = -2
    AvailabilityCode = Literal[1, 0, -1, -2]

    @staticmethod
    def parselist() -> list[str]:
        """
        Parse the list of names specified in the file path.

        Returns:
            list[str]: A list of names
        Raises:
            FileNotFoundError: If the file does not exist
        """

        if not os.path.exists(args.list):
            # Try to obtain it as a sibling from the script
            logging.warning("File not found, trying to find it as a sibling...")
            args.list = os.path.join(os.path.dirname(__file__), args.list)
        with open(args.list, "r") as f:
            return f.read().splitlines()
    
    @staticmethod
    def islegal(name: str) -> bool:
        return all(c in NameFinder.LEGAL_CHARS for c in name.lower()) and 3 <= len(name) <= 16

    @classmethod
    def isavailable(cls, name: str) -> AvailabilityCode:

        if not NameFinder.islegal(name):
            return cls.ILLEGAL

        response = requests.get(cls.URL.format(name))
        status_code = response.status_code

        if status_code != 429:
            logging.debug(f"NAME {name} STATUS {status_code} JSON {response.json()}")

        match status_code:
            case 200:
                return cls.UNAVAILABLE
            case 404:
                return cls.AVAIALBLE
            case 429:
                logging.error("API rate limit exceeded. Please try again later.")
                return cls.UNKNOWN
            case 402:
                return cls.AVAIALBLE
            case _:
                logging.error(f"Unknown status code: {status_code}")
                return cls.UNKNOWN
    
    @staticmethod
    def isavailable_threaded(names: list[str]) -> list[AvailabilityCode]:
        """
        Check availability of multiple names using concurrent.futures.

        Args:
            names (list[str]): List of names to check

        Returns:
            list[AvailabilityCode]: List of int codes indicating availability
        """
        with concurrent.futures.ThreadPoolExecutor() as executor:
            # Submit all tasks and get futures
            futures = [executor.submit(NameFinder.isavailable, name) for name in names]

            # Collect results as they complete, but maintain order
            results = [None] * len(names)  # Pre-allocate list to maintain order
            for future in concurrent.futures.as_completed(futures):
                # Find which index this future corresponds to
                for i, f in enumerate(futures):
                    if f == future:
                        results[i] = future.result() #type: ignore
                        break

        return results #type: ignore

    @staticmethod
    def format_result(name: str, available: AvailabilityCode) -> str:
        results = {
            NameFinder.AVAIALBLE:       GREEN + "Available",
            NameFinder.UNAVAILABLE:     RED + "Unavailable",
            NameFinder.UNKNOWN:         YELLOW + "Unknown",
            NameFinder.ILLEGAL:         ORANGE + "Illegal"
        }

        if len(name) > 16:
            return f"{(name[:13] + "...").ljust(17)}{results[available]}{RESET}"
        return f"{name.ljust(17)}{results[available]}{RESET}"

    @staticmethod
    def save_results(names: list[str], availability: list[AvailabilityCode]):
        if args.output is None:
            logging.warning("No output file specified, skipping save.")
            return

        # Create directory if it doesn't exist
        output_dir = os.path.dirname(args.output)
        if output_dir and not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)

        with open(args.output, "w") as f:
            for name, available in zip(names, availability):
                if available == NameFinder.AVAIALBLE:
                    logging.debug(f"Saving result for {name}")
                    f.write(name + "\n")

def main():

    if args.list:
        try:
            namelist = NameFinder.parselist()
        except FileNotFoundError:
            logging.error(f"File \"{args.list}\" not found.")
            exit(1)

        availability = NameFinder.isavailable_threaded(namelist)

        for i, (name, available) in enumerate(zip(namelist, availability)):
            print(NameFinder.format_result(name, available), end="\t")
            if i % 10 == 9:
                print()
        print()
        if args.output:
            NameFinder.save_results(namelist, availability)
    else:
        available = NameFinder.isavailable(args.name)
        print(NameFinder.format_result(args.name, available))



if __name__ == "__main__":
    main()