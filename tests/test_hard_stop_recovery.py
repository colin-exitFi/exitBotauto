import json
import unittest

from src.main import TradingBot


class _FakeBroker:
    def place_stop_loss_order(self, *args, **kwargs):
        return None

    def pop_order_error(self, client_order_id):
        return {
            "status_code": 422,
            "body": json.dumps(
                {
                    "market_price": "147.84",
                    "message": "stop price must be greater than current price",
                    "stop_price": "147.80",
                }
            ),
        }


class HardStopRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_crossed_short_hard_stop_falls_back_to_software_exit(self):
        bot = TradingBot.__new__(TradingBot)
        bot.alpaca_client = _FakeBroker()
        bot._entry_session_label = lambda: "regular"
        calls = []

        async def _submit(position, current_price, reason):
            calls.append((position["symbol"], current_price, reason))
            return True

        bot._submit_software_managed_exit = _submit

        position = {
            "symbol": "NRG",
            "side": "short",
            "quantity": 4.0,
            "entry_price": 145.62,
            "hard_stop_price": 147.80,
        }

        await TradingBot._ensure_hard_stop(bot, position, {}, 147.84)

        self.assertEqual(position["order_state"]["hard_stop"], "software_exit")
        self.assertEqual(calls, [("NRG", 147.84, "hard_stop")])
