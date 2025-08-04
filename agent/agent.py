import os
import time
import re
import logging
import json
import asyncio
from eth_abi import decode
from dotenv import load_dotenv
from datetime import datetime, timedelta
import aiohttp
from web3.exceptions import ContractLogicError
from web3 import AsyncWeb3
from web3 import Web3
from web3.providers.persistent import WebSocketProvider


def escape_markdown(text):
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('arbitrage_agent.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
WS_URL = os.getenv("WS_URL")
PRIVATE_KEY = os.getenv('PRIVATE_KEY')
CONTRACT_ADDRESS = os.getenv('CONTRACT_ADDRESS')
WALLET_ADDRESS = os.getenv('WALLET_ADDRESS')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

if not all([WS_URL, PRIVATE_KEY, CONTRACT_ADDRESS, WALLET_ADDRESS, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
    logger.error("Missing environment variables")
    exit(1)

# Globals
w3: AsyncWeb3 = None
contract = None
PAIRS = []
WETH = None

# Constants
LOAN_AMOUNTS = []
DEADLINE_DELTA = 300  # 5 minutes
MIN_PROFIT_THRESHOLD = None  # 0.01 WETH
MAX_GAS_PRICE = None
BASE_PRIORITY_FEE = None
POLL_INTERVAL = 1  # Seconds
MAX_BACKOFF = 600
INITIAL_BACKOFF = 10
GAS_BUFFER = 1.2  # 20% buffer


# Load contract ABI
try:
    with open('PrimeFlashArb.json', 'r') as f:
        contract_metadata = json.load(f)
        contract_abi = contract_metadata['abi']
except Exception as e:
    logger.error(f"Failed to load ABI: {str(e)}")
    exit(1)

# Nonce management
nonce_lock = asyncio.Lock()
last_nonce = None

# Telegram alert
async def send_telegram_alert(message):
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            escaped_message = escape_markdown(message)
            payload = {
                'chat_id': TELEGRAM_CHAT_ID,
                'text': escaped_message,
                'parse_mode': 'MarkdownV2'
            }
            async with session.post(url, json=payload) as response:
                if response.status != 200:
                    logger.error(f"Failed to send Telegram alert: {await response.text()}")
    except Exception as e:
        logger.error(f"Telegram alert error: {str(e)}")

async def build_deadline():
    return int((datetime.now() + timedelta(seconds=DEADLINE_DELTA)).timestamp())

async def get_revert_reason(tx_hash):
    try:
        tx = await w3.eth.get_transaction(tx_hash)
        result = await w3.eth.call(tx, block_identifier=tx['blockNumber'])
        try:
            decoded = decode(['string'], result[4:])
            return decoded[0]
        except:
            try:
                decoded = decode(['bytes'], result[4:])
                return f"Bytes: {decoded[0].hex()}"
            except:
                return f"Raw data: {result.hex()}"
    except ContractLogicError as e:
        return str(e)
    except Exception as e:
        return f"Error fetching revert reason: {str(e)}"

async def get_revert_reason_from_simulation(pair, amount_in):
    try:
        fn = contract.get_function_by_name('simulateArbitrage')(
            WETH,
            pair['uniswap_pair'],
            pair['sushiswap_pair'],
            pair['path1'],
            pair['path2'],
            amount_in
        )
        tx = {
            'to': CONTRACT_ADDRESS,
            'from': WALLET_ADDRESS,
            'data': fn._encode_transaction_data(),
        }
        result = await w3.eth.call(tx)
        return decode(['string'], result[4:])[0]
    except Exception as e:
        return f"Could not decode revert reason: {str(e)}"
    
async def simulate_arbitrage(pair, amount_in):
    try:
        # Get amounts out for fee estimation
        router_abi = [{
            "name": "getAmountsOut",
            "type": "function",
            "inputs": [
                {"name": "amountIn", "type": "uint256"},
                {"name": "path", "type": "address[]"},
            ],
            "outputs": [{"name": "amounts", "type": "uint256[]"}],
            "constant": True,
            "stateMutability": "view",
        }]
        uniswap_router = w3.eth.contract(address=w3.to_checksum_address('0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D'), abi=router_abi)
        sushiswap_router = w3.eth.contract(address=w3.to_checksum_address('0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F'), abi=router_abi)
        out1 = await uniswap_router.functions.getAmountsOut(amount_in, pair['path1']).call()
        token_out = out1[-1]
        out2 = await sushiswap_router.functions.getAmountsOut(token_out, pair['path2']).call()
        weth_out = out2[-1]

        # Estimate fees (0.3% per swap)
        fee_step1 = token_out * 0.003
        fee_step2 = weth_out * 0.003
        #total_fees_wei = int(fee_step1 * (10**18 / 10**pair['decimals']) + fee_step2)
        #total_fees_weth = total_fees_wei / 10**18

        # Assume token_out has `pair['decimals']`, weth_out in wei
        fee_token_wei = token_out * 0.003
        fee_weth_wei = weth_out * 0.003
        total_fees_wei = int(fee_token_wei + fee_weth_wei)
        total_fees_weth = total_fees_wei / 1e18
        # Simulate arbitrage
        profitable, estimated_profit = await contract.functions.simulateArbitrage(
            WETH,
            pair['uniswap_pair'],
            pair['sushiswap_pair'],
            pair['path1'],
            pair['path2'],
            amount_in
        ).call()
        # Estimate gas
        fn = contract.get_function_by_name('simulateArbitrage')(
            WETH,
            pair['uniswap_pair'],
            pair['sushiswap_pair'],
            pair['path1'],
            pair['path2'],
            amount_in
        )
        tx = {
            'to': CONTRACT_ADDRESS,
            'from': WALLET_ADDRESS,
            'data': fn._encode_transaction_data(),  # no arguments here!
        }
        try:
            gas_estimate = await w3.eth.estimate_gas(tx)
            gas_price = await w3.eth.gas_price
            gas_cost_wei = int(gas_estimate * GAS_BUFFER * gas_price)
            gas_cost_weth = gas_cost_wei / 10**18
        except Exception as e:
            logger.warning(f"Gas estimation failed for {pair['name']}, amount {w3.from_wei(amount_in, 'ether')} WETH: {e}")
            gas_cost_weth = 0.01  # Fallback estimate

        # Calculate net profit
        gross_profit_weth = w3.from_wei(estimated_profit, 'ether')
        net_profit_weth = gross_profit_weth - total_fees_weth - gas_cost_weth
        logger.info(
            f"Simulation ({pair['name']}, Amount={w3.from_wei(amount_in, 'ether')} WETH): "
            f"Profitable={profitable}, Gross Profit={gross_profit_weth:.6f} WETH, "
            f"Fees={total_fees_weth:.6f} WETH, Gas={gas_cost_weth:.6f} WETH, "
            f"Net Profit={net_profit_weth:.6f} WETH"
        )
        print("token_out: ",token_out)
        print("weth_out: ",weth_out)
        print("fee_step1: ",fee_step1)
        print("fee_step2: ",fee_step2)
        print("total_fees_wei: ",total_fees_wei)
        print("total_fees_weth: ",total_fees_weth)
        print("gas_estimate: ",gas_estimate)
        print("gas_price: ",gas_price)
        print("gas_cost_wei: ",gas_cost_wei)
        print("gas_cost_weth: ",gas_cost_weth)
        
        return profitable, estimated_profit, net_profit_weth, gas_cost_weth
    except Exception as e:
        reason = await get_revert_reason_from_simulation(pair, amount_in)
        
        logger.error(f"Simulation failed ({pair['name']}, Amount={w3.from_wei(amount_in, 'ether')} WETH): {reason}")
        await send_telegram_alert(f"⚠️ Simulation failed: {reason.replace('(', '\\(').replace(')', '\\)')}")
        return False, 0, 0, 0

async def execute_arbitrage():
    global last_nonce
    best_pair = None
    best_amount = None
    best_profit = 0
    best_net_profit = -float('inf')
    best_profitable = False
    best_gas_cost = 0

    for pair in PAIRS:
        for amount_in in LOAN_AMOUNTS:
            await asyncio.sleep(1)  # Throttle to ~6.6 requests/sec (well under the 15 limit)
            profitable, estimated_profit, net_profit_weth, gas_cost_weth = await simulate_arbitrage(pair, amount_in)
            #print(profitable, estimated_profit, net_profit_weth, gas_cost_weth)
            await send_telegram_alert(
                f"🧠 Profit Opportunity: {pair['name']}, Amount={w3.from_wei(amount_in, 'ether')} WETH, "
                f"Net Profit={net_profit_weth:.8f} WETH"
            )
            if (profitable and 
                estimated_profit >= MIN_PROFIT_THRESHOLD and 
                net_profit_weth > best_net_profit and 
                #net_profit_weth > w3.from_wei(MIN_PROFIT_THRESHOLD, 'ether')):
                net_profit_weth > 0):
                best_pair = pair
                best_amount = amount_in
                best_profit = estimated_profit
                best_net_profit = net_profit_weth
                best_profitable = True
                best_gas_cost = gas_cost_weth

    if not best_profitable:
        logger.info("No profitable opportunity or net profit below threshold")
        return False

    # Check gas prices
    base_fee = (await w3.eth.get_block('latest'))['baseFeePerGas']
    priority_fee = min(BASE_PRIORITY_FEE, await w3.eth.max_priority_fee)
    max_fee = base_fee + priority_fee
    if max_fee > MAX_GAS_PRICE:
        logger.warning(f"Max fee too high: {w3.from_wei(max_fee, 'gwei')} gwei")
        return False

    try:
        async with nonce_lock:
            if last_nonce is None:
                last_nonce = await w3.eth.get_transaction_count(WALLET_ADDRESS, 'pending')
            else:
                last_nonce += 1

            tx = await contract.functions.executeArbitrage(
                WETH,
                best_pair['uniswap_pair'],
                best_pair['sushiswap_pair'],
                best_pair['path1'],
                best_pair['path2'],
                best_amount,
                await build_deadline()
            ).build_transaction({
                'from': WALLET_ADDRESS,
                'maxFeePerGas': max_fee,
                'maxPriorityFeePerGas': priority_fee,
                'nonce': last_nonce,
                'chainId': await w3.eth.chain_id
            })

            gas_estimate = await w3.eth.estimate_gas(tx)
            tx['gas'] = int(gas_estimate * GAS_BUFFER)

            signed_tx = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
            tx_hash = await w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            logger.info(f"Transaction sent: {tx_hash.hex()} (Net Profit={best_net_profit:.6f} WETH)")
            await send_telegram_alert(f"🚀 Transaction sent: `{tx_hash.hex()}`")

            receipt = await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt.status == 1:
                logger.info("Arbitrage executed successfully")
                await send_telegram_alert(f"✅ Arbitrage executed successfully: `{tx_hash.hex()}`")
            else:
                revert_reason = await get_revert_reason(tx_hash)
                logger.error(f"Transaction failed: {revert_reason}")
                await send_telegram_alert(f"❌ Transaction failed: `{revert_reason}`")
                last_nonce -= 1
            return receipt.status == 1
    except Exception as e:
        logger.error(f"Execution failed: {str(e)}")
        await send_telegram_alert(f"❌ Execution failed: {str(e)}")
        async with nonce_lock:
            last_nonce = await w3.eth.get_transaction_count(WALLET_ADDRESS, 'pending')
        return False


async def handle_new_block(block_number):
    logger.info(f"New block: {block_number}")
    try:
        success = await execute_arbitrage()
        return success, 0
    except Exception as e:
        logger.error(f"Block handling error: {str(e)}")
        await send_telegram_alert(f"⚠️ Block handling error: {str(e)}")
        return False, INITIAL_BACKOFF

async def main():
    global w3, contract,WETH, PAIRS, LOAN_AMOUNTS, MIN_PROFIT_THRESHOLD

    provider = WebSocketProvider(WS_URL)
    await provider.connect()  # 🔧 This line is required to establish the connection

    w3 = AsyncWeb3(provider) 

    contract = w3.eth.contract(
        address=Web3.to_checksum_address(CONTRACT_ADDRESS),
        abi=contract_abi
    )
    # Re-initialize data that used w3 before it was ready
    # Token and pair addresses
    WETH = Web3.to_checksum_address('0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2')
    DAI = Web3.to_checksum_address('0x6B175474E89094C44Da98b954EedeAC495271d0F')
    USDT = Web3.to_checksum_address('0xdAC17F958D2ee523a2206206994597C13D831ec7')
    USDC = Web3.to_checksum_address('0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48')
    UNI = Web3.to_checksum_address('0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984')

    # Pair configurations
    PAIRS = [
        {
            'name': 'WETH/USDT',
            'token': USDT,
            'token_name': 'USDT',
            'uniswap_pair': w3.to_checksum_address('0x0d4a11d5EEaaC28EC3F61d100daF4d40471f1852'),
            'sushiswap_pair': w3.to_checksum_address('0x06da0fd433C1A5d7a4faa01111c044910A184553'),
            'path1': [WETH, USDT],
            'path2': [USDT, WETH],
            'decimals': 6
        },
        {
            'name': 'USDC/WETH',
            'token': USDC,
            'token_name': 'USDC',
            'uniswap_pair': w3.to_checksum_address('0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc'),
            'sushiswap_pair': w3.to_checksum_address('0x397FF1542f962076d0BFE58eA045FfA2d347ACa0'),
            'path1': [WETH, USDC],
            'path2': [USDC, WETH],
            'decimals': 6
        },
    ]

    # Constants
    LOAN_AMOUNTS = [
        w3.to_wei(1, 'ether'),  # 0.01 WETH
        w3.to_wei(10, 'ether'),   # 0.1 WETH
    ]
    DEADLINE_DELTA = 300  # 5 minutes
    MIN_PROFIT_THRESHOLD = w3.to_wei(0.000001, 'ether')  # 0.01 WETH
    MAX_GAS_PRICE = w3.to_wei(100, 'gwei')
    BASE_PRIORITY_FEE = w3.to_wei(2, 'gwei')
    POLL_INTERVAL = 1  # Seconds
    MAX_BACKOFF = 600
    INITIAL_BACKOFF = 10
    GAS_BUFFER = 1.2  # 20% buffer

    logger.info("Starting arbitrage agent")
    await send_telegram_alert("🟢 Arbitrage agent started")
    try:
        last_block = await w3.eth.block_number
        logger.info(f"Last block: {last_block}")
    except Exception as e:
        logger.error(f"Failed to fetch block number: {e}")
        return
    while True:
        current_block = await w3.eth.block_number
        if current_block > last_block:
            last_block = current_block
            success, backoff = await handle_new_block(current_block)
            if not success and backoff:
                await asyncio.sleep(backoff)
        else:
            await asyncio.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    asyncio.run(main())