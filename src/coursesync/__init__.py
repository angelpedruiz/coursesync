"""coursesync: fetch new course material (recordings, Moodle, websites, repos) into tidy course folders."""

from coursesync.config import Config, load
from coursesync.run import Manifest, run

__all__ = ["Config", "Manifest", "load", "run"]
__version__ = "0.1.0"
