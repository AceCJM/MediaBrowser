import logging


class SuppressBrokenPipeFilter(logging.Filter):
    """
    Filter to suppress "Broken pipe" error messages that occur when
    clients disconnect during media streaming.
    """

    def filter(self, record):
        # Suppress broken pipe errors from WSGI server
        if hasattr(record, 'msg') and 'Broken pipe' in str(record.msg):
            return False
        return True