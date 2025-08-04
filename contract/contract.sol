// SPDX-License-Identifier: MIT
pragma solidity ^0.8.18;

import "@openzeppelin/contracts/security/ReentrancyGuard.sol";
import "@chainlink/contracts/src/v0.8/interfaces/AggregatorV3Interface.sol";

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
    function approve(address spender, uint256 amount) external returns (bool);
    function transfer(address recipient, uint256 amount) external returns (bool);
}

interface IUniswapV2Router {
    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        address[] calldata path,
        address to,
        uint256 deadline
    ) external returns (uint256[] memory amounts);
    function getAmountsOut(uint256 amountIn, address[] calldata path) external view returns (uint256[] memory amounts);
}

interface IUniswapV2Pair {
    function getReserves() external view returns (uint112, uint112, uint32);
    function token0() external view returns (address);
}

interface IAavePool {
    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16 referralCode
    ) external;
}

interface IFlashLoanSimpleReceiver {
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external returns (bool);
}

contract PrimeFlashArb is IFlashLoanSimpleReceiver, ReentrancyGuard {
    address public immutable owner ;
    IAavePool public immutable AAVE_POOL;
    IUniswapV2Router public immutable router1; // e.g., Uniswap
    IUniswapV2Router public immutable router2; // e.g., Sushiswap
    IERC20 public immutable WETH;
    AggregatorV3Interface public immutable ethPriceFeed;
    AggregatorV3Interface public immutable gasPriceFeed;

    uint256 public minProfit = 0.01 ether;
    uint256 public slippageBps = 50; // 0.5%
    uint256 public gasEstimate = 500_000;
    uint256 public maxGasPrice = 100 gwei;
    uint256 public flashLoanFeeBps = 9; // 0.09% Aave fee
    uint256 public priceDeviationBps = 50; // 0.5% price deviation

    event ArbitrageExecuted(uint256 profit, address[] path1, address[] path2);
    event ArbitrageFailed(string reason, uint256 balance, uint256 amountOwed);
    event Withdrawn(address token, uint256 amount);
    event ParametersUpdated(
        uint256 minProfit,
        uint256 slippageBps,
        uint256 gasEstimate,
        uint256 maxGasPrice,
        uint256 flashLoanFeeBps,
        uint256 priceDeviationBps
    );

    constructor(
        address _aavePool,
        address _router1,
        address _router2,
        address _weth,
        address _ethPriceFeed,
        address _gasPriceFeed
    ) {
        owner = msg.sender;
        AAVE_POOL = IAavePool(_aavePool);
        router1 = IUniswapV2Router(_router1);
        router2 = IUniswapV2Router(_router2);
        WETH = IERC20(_weth);
        ethPriceFeed = AggregatorV3Interface(_ethPriceFeed);
        gasPriceFeed = AggregatorV3Interface(_gasPriceFeed);

        WETH.approve(_router1, type(uint256).max);
        WETH.approve(_router2, type(uint256).max);
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "Not owner");
        _;
    }

    // Update configurable parameters
    function updateParameters(
        uint256 _minProfit,
        uint256 _slippageBps,
        uint256 _gasEstimate,
        uint256 _maxGasPrice,
        uint256 _flashLoanFeeBps,
        uint256 _priceDeviationBps
    ) external onlyOwner {
        require(_slippageBps <= 500, "Slippage too high"); // Max 5%
        require(_maxGasPrice <= 500 gwei, "Gas price too high");
        require(_flashLoanFeeBps <= 50, "Fee too high"); // Max 0.5%
        require(_priceDeviationBps <= 500, "Deviation too high"); // Max 5%
        minProfit = _minProfit;
        slippageBps = _slippageBps;
        gasEstimate = _gasEstimate;
        maxGasPrice = _maxGasPrice;
        flashLoanFeeBps = _flashLoanFeeBps;
        priceDeviationBps = _priceDeviationBps;
        emit ParametersUpdated(_minProfit, _slippageBps, _gasEstimate, _maxGasPrice, _flashLoanFeeBps, _priceDeviationBps);
    }

    // Get Chainlink price
    function getChainlinkPrice(AggregatorV3Interface feed) internal view returns (uint256) {
        (, int256 price,,,) = feed.latestRoundData();
        require(price > 0, "Invalid price");
        return uint256(price);
    }

    // Get estimated gas cost
    function getGasCost() public view returns (uint256) {
        uint256 gasPrice = getChainlinkPrice(gasPriceFeed) / 1e9; // Gas price feed typically in Gwei
        return gasPrice * gasEstimate;
    }

    // Calculate amount out for a swap
    function getAmountOut(
        uint256 amountIn,
        uint256 reserveIn,
        uint256 reserveOut
    ) internal pure returns (uint256) {
        if (amountIn == 0 || reserveIn == 0 || reserveOut == 0) return 0;
        uint256 amountInWithFee = amountIn * 997; // 0.3% fee
        uint256 numerator = amountInWithFee * reserveOut;
        uint256 denominator = reserveIn * 1000 + amountInWithFee;
        return numerator / denominator;
    }

    // Check pool reserves
    function checkReserves(
        address pair,
        uint256 amountIn,
        address tokenIn
    ) internal view returns (bool) {
        (uint112 reserve0, uint112 reserve1,) = IUniswapV2Pair(pair).getReserves();
        address token0 = IUniswapV2Pair(pair).token0();
        (uint112 reserveIn, uint112 reserveOut) = tokenIn == token0 ? (reserve0, reserve1) : (reserve1, reserve0);
        uint256 amountOut = getAmountOut(amountIn, reserveIn, reserveOut);
        return amountOut > 0 && reserveOut >= amountOut * 2; // Require 2x liquidity
    }

    // Validate trade price against Chainlink
    function validateTrade(
        uint256 amountIn,
        uint256 expectedOut,
        address[] memory path,
        address token
    ) internal view returns (bool) {
        if (path[path.length - 1] != address(WETH) || token != address(WETH)) return true; // Skip for non-WETH
        uint256 ethPrice = getChainlinkPrice(ethPriceFeed);
        uint256 expectedValue = (amountIn * ethPrice) / 1e18;
        return expectedOut >= (expectedValue * (10_000 - priceDeviationBps)) / 10_000;
    }

    // Simulate arbitrage
    function simulateArbitrage(
        address token,
        address pair1,
        address pair2,
        address[] memory path1,
        address[] memory path2,
        uint256 amountIn
    ) public view returns (bool profitable, uint256 estimatedProfit) {
        require(path1.length >= 2 && path2.length >= 2, "Invalid paths");
        require(path1[0] == token && path2[path2.length - 1] == token, "Invalid token paths");
        require(tx.gasprice <= maxGasPrice, "Gas too high");

        // Check reserves
        if (!checkReserves(pair1, amountIn, path1[0]) || !checkReserves(pair2, amountIn, path2[0])) {
            return (false, 0);
        }

        // Simulate swaps
        uint256[] memory out1 = router1.getAmountsOut(amountIn, path1);
        if (out1.length < 2) return (false, 0);

        uint256[] memory out2 = router2.getAmountsOut(out1[out1.length - 1], path2);
        if (out2.length < 2) return (false, 0);

        uint256 finalOut = out2[out2.length - 1];
        uint256 premium = (amountIn * flashLoanFeeBps) / 10_000;
        uint256 totalOwed = amountIn + premium;
        uint256 gasCost = getGasCost();

        if (finalOut > totalOwed + gasCost + minProfit) {
            return (true, finalOut - totalOwed - gasCost);
        }
        return (false, 0);
    }

    // Execute arbitrage
    function executeArbitrage(
        address token,
        address pair1,
        address pair2,
        address[] calldata path1,
        address[] calldata path2,
        uint256 amountIn,
        uint256 deadline
    ) external onlyOwner nonReentrant {
        require(tx.gasprice <= maxGasPrice, "Gas too high");
        require(block.timestamp <= deadline, "Trade expired");
        require(path1.length >= 2 && path2.length >= 2, "Invalid paths");
        require(path1[0] == token && path2[path2.length - 1] == token, "Invalid token paths");

        (bool profitable,) = simulateArbitrage(token, pair1, pair2, path1, path2, amountIn);
        require(profitable, "Not profitable");

        bytes memory params = abi.encode(token, pair1, pair2, path1, path2, amountIn, deadline);
        AAVE_POOL.flashLoanSimple(address(this), token, amountIn, params, 0);
    }

    // Struct to store flash loan parameters
    struct FlashLoanParams {
        address token;
        address pair1;
        address pair2;
        address[] path1;
        address[] path2;
        uint256 amountIn;
        uint256 deadline;
    }

    // Helper function for first swap
    function _executeFirstSwap(
        FlashLoanParams memory params,
        IERC20 token
    ) internal returns (uint256[] memory amounts) {
        require(checkReserves(params.pair1, params.amountIn, params.path1[0]), "Insufficient liquidity in pair1");
        uint256[] memory out = router1.getAmountsOut(params.amountIn, params.path1);
        uint256 outMin = (out[out.length - 1] * (10_000 - slippageBps)) / 10_000;
        require(validateTrade(params.amountIn, outMin, params.path1, params.token), "Invalid trade price");
        token.approve(address(router1), params.amountIn);
        amounts = router1.swapExactTokensForTokens(
            params.amountIn,
            outMin,
            params.path1,
            address(this),
            params.deadline
        );
    }

    // Helper function for second swap
    function _executeSecondSwap(
        FlashLoanParams memory params,
        IERC20 intermediateToken,
        uint256 intermediateBalance
    ) internal returns (uint256[] memory amounts) {
        require(checkReserves(params.pair2, intermediateBalance, params.path2[0]), "Insufficient liquidity in pair2");
        uint256[] memory out = router2.getAmountsOut(intermediateBalance, params.path2);
        uint256 outMin = (out[out.length - 1] * (10_000 - slippageBps)) / 10_000;
        intermediateToken.approve(address(router2), intermediateBalance);
        amounts = router2.swapExactTokensForTokens(
            intermediateBalance,
            outMin,
            params.path2,
            address(this),
            params.deadline
        );
    }

    // Flash loan callback
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external override returns (bool) {
        require(msg.sender == address(AAVE_POOL), "Invalid caller");
        require(initiator == address(this), "Invalid initiator");
        require(asset == abi.decode(params, (FlashLoanParams)).token, "Invalid asset");

        FlashLoanParams memory flashParams = abi.decode(params, (FlashLoanParams));
        require(block.timestamp <= flashParams.deadline, "Trade expired");

        IERC20 token = IERC20(flashParams.token);

        // First swap
        _executeFirstSwap(flashParams, token);

        // Second swap
        IERC20 intermediateToken = IERC20(flashParams.path1[flashParams.path1.length - 1]);
        uint256 intermediateBalance = intermediateToken.balanceOf(address(this));
        _executeSecondSwap(flashParams, intermediateToken, intermediateBalance);

        // Calculate profitability
        uint256 totalOwed = amount + premium;
        uint256 finalBalance = token.balanceOf(address(this));
        uint256 gasCost = getGasCost();

        if (finalBalance < totalOwed + gasCost + minProfit) {
            token.approve(address(router1), 0);
            intermediateToken.approve(address(router2), 0);
            emit ArbitrageFailed("Insufficient profit", finalBalance, totalOwed);
            return false;
        }

        // Repay flash loan
        token.approve(address(AAVE_POOL), totalOwed);

        // Transfer profit
        uint256 profit = finalBalance - totalOwed - gasCost;
        require(token.transfer(owner, profit), "Profit transfer failed");
        emit ArbitrageExecuted(profit, flashParams.path1, flashParams.path2);

        // Reset approvals
        token.approve(address(router1), 0);
        intermediateToken.approve(address(router2), 0);

        return true;
    }

    // Withdraw tokens
    function withdraw(address token, uint256 amount) external onlyOwner {
        require(IERC20(token).transfer(owner, amount), "Transfer failed");
        emit Withdrawn(token, amount);
    }

    // Fallback to receive ETH
    receive() external payable {}
}
