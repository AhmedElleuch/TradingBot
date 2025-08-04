 (cd "$(git rev-parse --show-toplevel)" && git apply --3way <<'EOF' 
diff --git a//dev/null b/readme.md
index 0000000000000000000000000000000000000000..afe90940e02272d0ce97de927c0bd053fde72624 100644
--- a//dev/null
+++ b/readme.md
@@ -0,0 +1,28 @@
+# TradingBot
+
+A Python-based arbitrage bot that listens for new Ethereum blocks and executes flash-loan backed swaps across decentralized exchanges.
+
+## Setup
+
+1. **Install dependencies**
+   ```bash
+   pip install web3 eth-abi aiohttp python-dotenv
+   ```
+
+2. **Create a `.env` file** with the following variables:
+   ```text
+   WS_URL= # WebSocket endpoint for an Ethereum node
+   PRIVATE_KEY= # Private key of the wallet executing trades
+   CONTRACT_ADDRESS= # Deployed PrimeFlashArb contract address
+   WALLET_ADDRESS= # Wallet address corresponding to the private key
+   TELEGRAM_BOT_TOKEN= # Bot token for Telegram alerts
+   TELEGRAM_CHAT_ID= # Chat ID to receive Telegram alerts
+   ```
+
+3. **Run the arbitrage agent**
+   ```bash
+   python agent/agent.py
+   ```
+
+The bot will log activity to `arbitrage_agent.log` and notify the configured Telegram chat.
+
 
EOF
)