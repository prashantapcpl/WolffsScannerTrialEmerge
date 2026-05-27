"""
webhook_sender.py
Sends BUY, AVG and EXIT signals.
gap_fill=True means signal was missed while scanner was off.
"""

import requests
import json
import threading
import time
import os
from datetime import datetime
import pytz

IST = pytz.timezone("Asia/Kolkata")


def load_config():
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")
    with open(config_path, "r") as f:
        return json.load(f)


class WebhookSender:

    def __init__(self, scanner_id: str = "scanner_1", webhooks_cfg: dict = None):
        self.scanner_id = scanner_id
        self._webhooks  = webhooks_cfg or {}

    def _reload(self):
        try:
            config = load_config()
            scanners = config.get("scanners", {})
            if self.scanner_id in scanners:
                self._webhooks = scanners[self.scanner_id].get("webhooks", {})
        except:
            pass

    def _send(self, url: str, payload: dict, signal_type: str, retries: int = 3):
        if not url or url.strip() == "":
            return
        headers = {"Content-Type": "application/json"}
        for attempt in range(1, retries + 1):
            try:
                response = requests.post(url, data=json.dumps(payload),
                                         headers=headers, timeout=10)
                if response.status_code in (200, 201, 202, 204):
                    gap = " [GAP-FILL]" if payload.get("gap_fill") else ""
                    print(f"  📡 [{self.scanner_id}] {signal_type}{gap} webhook → [{response.status_code}]")
                    return
            except Exception as e:
                print(f"  ⚠️  [{self.scanner_id}] {signal_type} attempt {attempt}: {e}")
            if attempt < retries:
                time.sleep(2)

    def send_buy(self, symbol, plain_name, company_name,
                 buy_price, reference_price, drop_pct, rsi_at_watch,
                 gap_fill=False, signal_time=None):
        self._reload()
        url     = self._webhooks.get("buy_webhook_url", "")
        enabled = self._webhooks.get("enabled", True)
        if not enabled:
            return

        payload = {
            "signal":          "BUY",
            "scanner":         self.scanner_id,
            "label":           f"{plain_name} (BUY{'  MISSED' if gap_fill else ''})",
            "symbol":          symbol,
            "stock":           plain_name,
            "company":         company_name,
            "buy_price":       buy_price,
            "reference_price": reference_price,
            "drop_percent":    drop_pct,
            "rsi_at_watch":    rsi_at_watch,
            "avg_number":      0,
            "gap_fill":        gap_fill,
            "signal_time":     signal_time or datetime.now(IST).isoformat(),
            "sent_time":       datetime.now(IST).isoformat(),
            "note":            "MISSED SIGNAL — scanner was off" if gap_fill else "",
            "source":          f"FyersScanner/{self.scanner_id}"
        }
        threading.Thread(
            target=self._send, args=(url, payload, "BUY"), daemon=True
        ).start()

    def send_avg(self, symbol, plain_name, company_name,
                 avg_price, prev_avg_price, drop_pct, avg_num,
                 gap_fill=False, signal_time=None):
        self._reload()
        url     = self._webhooks.get("buy_webhook_url", "")
        enabled = self._webhooks.get("enabled", True)
        if not enabled:
            return

        payload = {
            "signal":         "AVG",
            "scanner":        self.scanner_id,
            "label":          f"{plain_name} (Avg {avg_num}{'  MISSED' if gap_fill else ''})",
            "symbol":         symbol,
            "stock":          plain_name,
            "company":        company_name,
            "avg_price":      avg_price,
            "prev_avg_price": prev_avg_price,
            "drop_percent":   drop_pct,
            "avg_number":     avg_num,
            "gap_fill":       gap_fill,
            "signal_time":    signal_time or datetime.now(IST).isoformat(),
            "sent_time":      datetime.now(IST).isoformat(),
            "note":           "MISSED SIGNAL — scanner was off" if gap_fill else "",
            "source":         f"FyersScanner/{self.scanner_id}"
        }
        threading.Thread(
            target=self._send, args=(url, payload, f"AVG{avg_num}"), daemon=True
        ).start()

    # ─── Sell-side (Phase 2) ──────────────────────────────────────────────
    def send_sell(self, symbol, plain_name, company_name,
                   short_price, reference_price, rise_pct, rsi_at_watch,
                   gap_fill=False, signal_time=None):
        self._reload()
        url     = self._webhooks.get("sell_webhook_url", "")
        enabled = self._webhooks.get("enabled", True)
        if not enabled:
            return
        payload = {
            "signal":          "SELL",
            "scanner":         self.scanner_id,
            "label":           f"{plain_name} (SELL{'  MISSED' if gap_fill else ''})",
            "symbol":          symbol,
            "stock":           plain_name,
            "company":         company_name,
            "short_price":     short_price,
            "reference_price": reference_price,
            "rise_percent":    rise_pct,
            "rsi_at_watch":    rsi_at_watch,
            "avg_number":      0,
            "side":            "SELL",
            "gap_fill":        gap_fill,
            "signal_time":     signal_time or datetime.now(IST).isoformat(),
            "sent_time":       datetime.now(IST).isoformat(),
            "note":            "MISSED SIGNAL — scanner was off" if gap_fill else "",
            "source":          f"FyersScanner/{self.scanner_id}",
        }
        threading.Thread(target=self._send,
                         args=(url, payload, "SELL"), daemon=True).start()

    def send_sell_avg(self, symbol, plain_name, company_name,
                       avg_price, prev_avg_price, rise_pct, avg_num,
                       gap_fill=False, signal_time=None):
        self._reload()
        url     = self._webhooks.get("sell_webhook_url", "")
        enabled = self._webhooks.get("enabled", True)
        if not enabled:
            return
        payload = {
            "signal":         "AVG_SELL",
            "scanner":        self.scanner_id,
            "label":          f"{plain_name} (Avg Short {avg_num}{'  MISSED' if gap_fill else ''})",
            "symbol":         symbol,
            "stock":          plain_name,
            "company":        company_name,
            "avg_price":      avg_price,
            "prev_avg_price": prev_avg_price,
            "rise_percent":   rise_pct,
            "avg_number":     avg_num,
            "side":           "SELL",
            "gap_fill":       gap_fill,
            "signal_time":    signal_time or datetime.now(IST).isoformat(),
            "sent_time":      datetime.now(IST).isoformat(),
            "source":         f"FyersScanner/{self.scanner_id}",
        }
        threading.Thread(target=self._send,
                         args=(url, payload, f"AVG_SELL{avg_num}"),
                         daemon=True).start()

    def send_sell_exit(self, symbol, plain_name, company_name,
                        exit_price, short_price, rsi_at_exit, avg_count=0,
                        gap_fill=False, signal_time=None):
        self._reload()
        url     = self._webhooks.get("sell_exit_webhook_url", "")
        enabled = self._webhooks.get("enabled", True)
        if not enabled:
            return
        pnl = round(((short_price - exit_price) / short_price) * 100, 2) \
              if short_price else None
        payload = {
            "signal":      "EXIT_SELL",
            "scanner":     self.scanner_id,
            "label":       f"{plain_name} (EXIT SELL{'  MISSED' if gap_fill else ''})",
            "symbol":      symbol,
            "stock":       plain_name,
            "company":     company_name,
            "exit_price":  exit_price,
            "short_price": short_price,
            "avg_count":   avg_count,
            "pnl_percent": pnl,
            "rsi_at_exit": rsi_at_exit,
            "side":        "SELL",
            "gap_fill":    gap_fill,
            "signal_time": signal_time or datetime.now(IST).isoformat(),
            "sent_time":   datetime.now(IST).isoformat(),
            "source":      f"FyersScanner/{self.scanner_id}",
        }
        threading.Thread(target=self._send,
                         args=(url, payload, "EXIT_SELL"), daemon=True).start()

    def send_sell_stoploss_exit(self, symbol, plain_name, company_name,
                                  exit_price, short_price, daily_rsi,
                                  avg_count=0, signal_time=None):
        self._reload()
        url     = self._webhooks.get("sell_exit_webhook_url", "")
        enabled = self._webhooks.get("enabled", True)
        if not enabled:
            return
        pnl = round(((short_price - exit_price) / short_price) * 100, 2) \
              if short_price else None
        payload = {
            "signal":      "EXIT_SELL_STOPLOSS",
            "scanner":     self.scanner_id,
            "label":       f"{plain_name} (EXIT SELL — Daily-RSI Stoploss)",
            "symbol":      symbol,
            "stock":       plain_name,
            "company":     company_name,
            "exit_price":  exit_price,
            "short_price": short_price,
            "avg_count":   avg_count,
            "pnl_percent": pnl,
            "daily_rsi":   daily_rsi,
            "reason":      "daily_rsi_stoploss",
            "side":        "SELL",
            "signal_time": signal_time or datetime.now(IST).isoformat(),
            "sent_time":   datetime.now(IST).isoformat(),
            "source":      f"FyersScanner/{self.scanner_id}",
        }
        threading.Thread(target=self._send,
                         args=(url, payload, "EXIT_SELL_STOPLOSS"),
                         daemon=True).start()

    def send_stoploss_exit(self, symbol, plain_name, company_name,
                           exit_price, buy_price, daily_rsi, avg_count=0,
                           signal_time=None):
        """Live daily-RSI stoploss exit — different payload + label so the
        external app can distinguish from a regular RSI-based exit."""
        self._reload()
        url     = self._webhooks.get("exit_webhook_url", "")
        enabled = self._webhooks.get("enabled", True)
        if not enabled:
            return

        pnl = round(((exit_price - buy_price) / buy_price) * 100, 2) \
              if buy_price else None
        payload = {
            "signal":      "EXIT_STOPLOSS",
            "scanner":     self.scanner_id,
            "label":       f"{plain_name} (EXIT — Daily-RSI Stoploss)",
            "symbol":      symbol,
            "stock":       plain_name,
            "company":     company_name,
            "exit_price":  exit_price,
            "buy_price":   buy_price,
            "avg_count":   avg_count,
            "pnl_percent": pnl,
            "daily_rsi":   daily_rsi,
            "reason":      "daily_rsi_stoploss",
            "signal_time": signal_time or datetime.now(IST).isoformat(),
            "sent_time":   datetime.now(IST).isoformat(),
            "source":      f"FyersScanner/{self.scanner_id}",
        }
        threading.Thread(
            target=self._send, args=(url, payload, "EXIT_STOPLOSS"),
            daemon=True
        ).start()

    def send_exit(self, symbol, plain_name, company_name,
                  exit_price, buy_price, rsi_at_exit, avg_count=0,
                  gap_fill=False, signal_time=None):
        self._reload()
        url     = self._webhooks.get("exit_webhook_url", "")
        enabled = self._webhooks.get("enabled", True)
        if not enabled:
            return

        payload = {
            "signal":      "EXIT",
            "scanner":     self.scanner_id,
            "label":       f"{plain_name} (EXIT{'  MISSED' if gap_fill else ''})",
            "symbol":      symbol,
            "stock":       plain_name,
            "company":     company_name,
            "exit_price":  exit_price,
            "buy_price":   buy_price,
            "avg_count":   avg_count,
            "pnl_percent": round(((exit_price-buy_price)/buy_price)*100,2) if buy_price else None,
            "rsi_at_exit": rsi_at_exit,
            "gap_fill":    gap_fill,
            "signal_time": signal_time or datetime.now(IST).isoformat(),
            "sent_time":   datetime.now(IST).isoformat(),
            "note":        "MISSED SIGNAL — scanner was off" if gap_fill else "",
            "source":      f"FyersScanner/{self.scanner_id}"
        }
        threading.Thread(
            target=self._send, args=(url, payload, "EXIT"), daemon=True
        ).start()
