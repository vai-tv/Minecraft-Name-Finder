import argparse
import logging
import os
import random
import requests
import time
import concurrent.futures

from tqdm import tqdm
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
handler.setFormatter(LevelFormatter(formats, datefmt="%H:%M:%S"))

logger = logging.getLogger()
# Set log level based on verbose flag - only show INFO and above unless verbose is enabled
logger.setLevel(logging.DEBUG if args.verbose else logging.INFO)
logger.addHandler(handler)

####################################################################################################


class NameFinder:

    URL = "https://api.mojang.com/users/profiles/minecraft/{}"
    BATCH_URL = "https://api.mojang.com/profiles/minecraft"
    BATCH_SIZE = 10
    BATCH_WAIT_TIME = 0.1

    LEGAL_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789_"

    AvailabilityCode = Literal[1, 0, -1, -2]
    AVAIALBLE: AvailabilityCode =    1
    UNAVAILABLE: AvailabilityCode =  0
    UNKNOWN: AvailabilityCode =     -1
    ILLEGAL: AvailabilityCode =     -2

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
                logging.error(f"API rate limit exceeded! Couldn't check {name}.")
                return cls.UNKNOWN
            case 402:
                return cls.AVAIALBLE
            case _:
                logging.error(f"Unknown status code: {status_code}")
                return cls.UNKNOWN
    
    @classmethod
    def isavailable_batch(cls, names: list[str]) -> list[AvailabilityCode]:
        results: list[NameFinder.AvailabilityCode] = [cls.UNKNOWN] * len(names)
        MAX_RETRIES = 5
        BASE_WAIT = 1.0  # seconds

        # top-level progress bar
        with tqdm(total=len(names), unit="names", desc="Checking names") as pbar:
            for i in range(0, len(names), cls.BATCH_SIZE):
                chunk = names[i:i+cls.BATCH_SIZE]
                legal_chunk = [n for n in chunk if cls.islegal(n)]

                # mark illegal names immediately
                for j, n in enumerate(chunk, start=i):
                    if not cls.islegal(n):
                        results[j] = cls.ILLEGAL
                        pbar.update(1)

                if not legal_chunk:
                    continue

                # try sending batch with retries
                retries = 0
                r = None
                while retries <= MAX_RETRIES:
                    try:
                        r = requests.post(cls.BATCH_URL, json=legal_chunk)
                    except requests.RequestException as e:
                        logging.error(f"Batch request failed: {e}")
                        wait = BASE_WAIT * (2 ** retries) + random.uniform(0, 0.5)
                        time.sleep(wait)
                        retries += 1
                        continue

                    if r.status_code == 200:
                        break  # success
                    elif r.status_code == 429:
                        # read Retry-After if Mojang sends it
                        retry_after = r.headers.get("Retry-After")
                        if retry_after:
                            wait = float(retry_after)
                        else:
                            # exponential backoff with jitter
                            wait = BASE_WAIT * (2 ** retries) + random.uniform(0, 0.5)
                        logging.warning(f"429 Rate limit hit. Waiting {wait:.2f}s before retrying batch {legal_chunk}.")
                        time.sleep(wait)
                        retries += 1
                        continue
                    elif r.status_code == 400:
                        logging.error(f"400 Bad Request: {legal_chunk}")
                        break
                    else:
                        logging.error(f"Unexpected status {r.status_code} for batch {legal_chunk}")
                        logging.error(r.text)
                        break

                # if we never succeeded, mark unknown
                if retries > MAX_RETRIES or r is None or r.status_code != 200:
                    for j, n in enumerate(chunk, start=i):
                        if results[j] != cls.ILLEGAL:
                            results[j] = cls.UNKNOWN
                            pbar.update(1)
                    continue

                # process successful response
                found_profiles = {p["name"].lower() for p in r.json()}
                for j, n in enumerate(chunk, start=i):
                    if results[j] == cls.ILLEGAL:
                        continue
                    results[j] = cls.UNAVAILABLE if n.lower() in found_profiles else cls.AVAIALBLE
                    pbar.update(1)

                # small delay to reduce likelihood of 429
                time.sleep(cls.BATCH_WAIT_TIME or 0.1)

        return results


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

        availability = NameFinder.isavailable_batch(namelist)

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