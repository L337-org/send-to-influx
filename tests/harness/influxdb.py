"""A stub InfluxDB: the ``/query`` and ``/write`` endpoints a control process uses.

Answers the v1 ``/query`` shape, which is also what a v2 server returns through its
v1-compatibility endpoint, so one stub serves both configurations the project supports.

The query is not parsed as InfluxQL. It is read for the quoted identifiers the project's
own builder puts in it - the fields between SELECT and FROM, and the measurement after FROM
- because a stub that reimplemented InfluxQL would be a second implementation to get wrong,
and what is being tested is the control process rather than the query language.

``frozen`` is the fault that matters most here, and it is subtle enough to be worth naming:
a source whose collector has stopped does not fail. It answers, promptly, with a value that
is no longer true. Freezing stops the timestamp advancing so the reading ages, which is the
only thing that tells a control the difference.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import re
import time

from tests.harness.endpoints import StubEndpoint

#: The quoted identifiers in a SELECT list, and the measurement after FROM.
_SELECT = re.compile(r'SELECT\s+(.*?)\s+FROM\s+"((?:[^"]|"")+)"', re.IGNORECASE | re.DOTALL)
_IDENTIFIER = re.compile(r'"((?:[^"]|"")+)"')


class StubInflux(StubEndpoint):
    """An InfluxDB holding one value per field, with a timestamp that can be frozen.

    Attributes:
        frozen (bool): answer with the timestamp each value was written at, rather than
            with now - a collector that has stopped, whose readings age.
        writes (list): the line-protocol bodies POSTed to ``/write``.
    """

    def __init__(self, readings=None):
        """Start an InfluxDB.

        Args:
            readings (dict or None): field name to value, as though already collected
        """
        super().__init__()
        self.frozen = False
        self.writes = []
        self._values = {}
        for field, value in (readings or {}).items():
            self.write_reading(field, value)

    def write_reading(self, field, value, at=None) -> None:
        """Store the value a query for this field will return.

        Args:
            field (str): the field key
            value (object): the value
            at (float or None): the unix time to record it at; now when None
        """
        with self.lock:
            self._values[field] = (value, time.time() if at is None else float(at))

    def age_reading(self, field, seconds) -> None:
        """Move one field's timestamp back, so it reads as that much older.

        Args:
            field (str): the field key
            seconds (float): how much older to make it

        Raises:
            KeyError: nothing has been written for that field
        """
        with self.lock:
            value, at = self._values[field]
            self._values[field] = (value, at - float(seconds))

    def respond(self, request):
        """Answer a query or accept a write.

        Args:
            request (Request): the request to answer

        Returns:
            tuple: the HTTP status and the body
        """
        if request.path.endswith("/write"):
            with self.lock:
                self.writes.append(request.body)
            return 204, {}
        if not request.path.endswith("/query"):
            return 404, {"error": f"no endpoint at {request.path}"}
        query = request.query.get("q", "")
        match = _SELECT.search(query)
        if not match:
            # A query this stub does not model answers with no series rather than an error:
            # "the measurement holds nothing" is a real answer a caller must handle, and an
            # error here would be the stub's opinion rather than InfluxDB's.
            return 200, {"results": [{"statement_id": 0}]}
        fields = [f.replace('""', '"') for f in _IDENTIFIER.findall(match.group(1))]
        measurement = match.group(2).replace('""', '"')
        return 200, {"results": [{"statement_id": 0, "series": self._series(measurement, fields)}]}

    def _series(self, measurement, fields):
        """Return the series for a SELECT, or an empty list where nothing is stored.

        Args:
            measurement (str): the measurement being read
            fields (list): the field keys in the SELECT list

        Returns:
            list: zero or one series, in the v1 response shape
        """
        with self.lock:
            known = [f for f in fields if f in self._values]
            if not known:
                return []
            stamps = [self._values[f][1] for f in known]
            # One row, one timestamp: a SELECT of several fields returns them side by side,
            # so the oldest of them is the age of the row as a whole.
            at = min(stamps) if self.frozen else time.time()
            row = [int(at)] + [self._values[f][0] for f in known]
        return [{"name": measurement, "columns": ["time"] + known, "values": [row]}]
