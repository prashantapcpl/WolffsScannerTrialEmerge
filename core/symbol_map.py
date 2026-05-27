"""
symbol_map.py
Converts plain stock names to Fyers API format.
Example: RELIANCE → NSE:RELIANCE-EQ
"""

import pandas as pd
import os
import json

def load_config():
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")
    with open(config_path, "r") as f:
        return json.load(f)

CONFIG    = load_config()
STOCKS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), CONFIG["stocks_file"])


class SymbolMapper:
    def __init__(self):
        self.df = None
        self.plain_to_fyers = {}
        self.fyers_to_plain = {}
        self.load()

    def load(self):
        """Load stocks CSV and build lookup dictionaries."""
        if not os.path.exists(STOCKS_FILE):
            raise FileNotFoundError(f"Stocks file not found: {STOCKS_FILE}")

        self.df = pd.read_csv(STOCKS_FILE)

        # Build both direction lookups
        for _, row in self.df.iterrows():
            plain  = str(row["plain_name"]).strip().upper()
            fyers  = str(row["fyers_symbol"]).strip()
            name   = str(row["company_name"]).strip()

            self.plain_to_fyers[plain] = {
                "fyers_symbol": fyers,
                "company_name": name
            }
            self.fyers_to_plain[fyers] = {
                "plain_name":   plain,
                "company_name": name
            }

        print(f"✅ Symbol map loaded: {len(self.plain_to_fyers)} stocks")

    def get_fyers_symbol(self, plain_name: str) -> str:
        """Convert plain name to Fyers symbol."""
        key = plain_name.strip().upper()
        result = self.plain_to_fyers.get(key)
        if result:
            return result["fyers_symbol"]
        # Try auto-format if not found
        auto = f"NSE:{key}-EQ"
        print(f"⚠️  '{plain_name}' not in map. Trying auto-format: {auto}")
        return auto

    def get_plain_name(self, fyers_symbol: str) -> str:
        """Convert Fyers symbol back to plain name."""
        result = self.fyers_to_plain.get(fyers_symbol)
        if result:
            return result["plain_name"]
        # Strip NSE: and -EQ if not found
        plain = fyers_symbol.replace("NSE:", "").replace("-EQ", "")
        return plain

    def get_company_name(self, plain_name: str) -> str:
        """Get full company name from plain name."""
        key = plain_name.strip().upper()
        result = self.plain_to_fyers.get(key)
        if result:
            return result["company_name"]
        return plain_name

    def get_all_fyers_symbols(self) -> list:
        """Return all Fyers symbols from the stocks file."""
        symbols = []
        for info in self.plain_to_fyers.values():
            sym = info["fyers_symbol"]
            # Skip index entries
            if "INDEX" not in sym:
                symbols.append(sym)
        return list(set(symbols))  # deduplicate

    def get_all_plain_names(self) -> list:
        """Return all plain stock names."""
        return list(self.plain_to_fyers.keys())

    def reload(self):
        """Reload the stocks file — useful when user edits stocks.csv."""
        self.plain_to_fyers = {}
        self.fyers_to_plain = {}
        self.load()
        print("🔄 Symbol map reloaded from stocks.csv")


# Singleton instance
_mapper = None

def get_mapper() -> SymbolMapper:
    global _mapper
    if _mapper is None:
        _mapper = SymbolMapper()
    return _mapper


if __name__ == "__main__":
    mapper = SymbolMapper()
    print("\n--- Test conversions ---")
    print(mapper.get_fyers_symbol("RELIANCE"))
    print(mapper.get_fyers_symbol("TCS"))
    print(mapper.get_plain_name("NSE:INFY-EQ"))
    print(f"\nTotal symbols loaded: {len(mapper.get_all_fyers_symbols())}")
