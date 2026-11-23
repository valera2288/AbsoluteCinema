import logging
import re


RESET_PATH = re.compile(r"(/account/reset/[^/\s]+/)[^/\s?]+/")


class RedactResetURL(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        if "/account/reset/" in message:
            record.msg = RESET_PATH.sub(r"\1[скрыто]/", message)
            record.args = ()
        return True
