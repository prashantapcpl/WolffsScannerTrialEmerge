"""
stock_groups.py — Named subsets of the symbol universe.

By convention:
  - The master file data/stocks.csv is automatically registered as the
    group "nifty650" (the default for every scanner).
  - Any *.csv file dropped into data/stock_groups/ is loaded as an
    additional named group, named after the file (e.g. nifty100.csv → "nifty100").

All group CSVs share the same schema as data/stocks.csv:
    plain_name,fyers_symbol,company_name

Membership lookups are case-sensitive on the Fyers symbol string
(e.g. "NSE:RELIANCE-EQ").
"""
import csv
import glob
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class StockGroups:

    DEFAULT_GROUP = "nifty650"
    GROUPS_DIR    = os.path.join(ROOT, "data", "stock_groups")

    def __init__(self):
        self._groups: dict[str, set[str]] = {}
        self.reload()

    def reload(self):
        """Re-scan disk.  Safe to call from the dashboard after a user
        uploads a new group CSV."""
        self._groups = {}

        master = os.path.join(ROOT, "data", "stocks.csv")
        if os.path.exists(master):
            self._groups[self.DEFAULT_GROUP] = self._load_csv(master)

        if os.path.isdir(self.GROUPS_DIR):
            for path in sorted(glob.glob(os.path.join(self.GROUPS_DIR, "*.csv"))):
                name = os.path.splitext(os.path.basename(path))[0]
                # Don't let a stray data/stock_groups/nifty650.csv shadow the master
                if name == self.DEFAULT_GROUP:
                    continue
                self._groups[name] = self._load_csv(path)

    @staticmethod
    def _load_csv(path: str) -> set:
        out: set = set()
        try:
            with open(path, "r", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    sym = (row.get("fyers_symbol")
                           or row.get("symbol")
                           or row.get("Symbol")
                           or "")
                    if sym:
                        out.add(sym.strip())
        except Exception as e:
            print(f"⚠️  stock_groups: failed loading {path}: {e}")
        return out

    def list(self) -> list:
        """All available group names, alphabetised, default group first."""
        names = sorted(self._groups.keys())
        if self.DEFAULT_GROUP in names:
            names.remove(self.DEFAULT_GROUP)
            names.insert(0, self.DEFAULT_GROUP)
        return names

    def symbols(self, name: str) -> set:
        return self._groups.get(name, set())

    def in_group(self, symbol: str, name: str) -> bool:
        # Unknown group name → return True (don't accidentally filter out
        # every symbol if config has a typo).  We log a warning in
        # ScannerInstance startup instead.
        grp = self._groups.get(name)
        if grp is None:
            return True
        return symbol in grp

    def size(self, name: str) -> int:
        return len(self._groups.get(name, set()))


# Module-level singleton
_singleton: StockGroups | None = None


def get_stock_groups() -> StockGroups:
    global _singleton
    if _singleton is None:
        _singleton = StockGroups()
    return _singleton
