"""
Market data integration with Twelve Data API and collaborative database caching
"""
import requests
import os
from typing import Dict, Optional, List, Any
from datetime import datetime, timedelta
import json
import logging
import random
import aiohttp

# Configure cleaner logging format
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

class MarketDataService:
    def __init__(self, db_service=None):
        # Twelve Data API configuration
        self.twelvedata_api_key = os.getenv("TWELVEDATA_API_KEY")
        self.twelvedata_base_url = "https://api.twelvedata.com"
        
        # Import database service
        if db_service is None:
            from database import db_service as default_db_service
            self.db_service = default_db_service
        else:
            self.db_service = db_service
        
        if not self.twelvedata_api_key:
            print("⚠️  Warning: TWELVEDATA_API_KEY not found in environment variables")
        else:
            print(f"✅ Twelve Data API configured (key: {self.twelvedata_api_key[:8]}...)")
    
    async def _get_cached_price(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get price from database cache if fresh enough with intelligent freshness"""
        try:
            # Check if we have fresh data with intelligent freshness logic
            is_fresh = await self.db_service.is_price_data_fresh(symbol, max_age_minutes=5)
            
            if is_fresh:
                cached_data = await self.db_service.get_current_price(symbol)
                if cached_data:
                    age_min = cached_data.get('cache_age_minutes', 0)
                    print(f"🎯 CACHE HIT  | {symbol:6} | ${cached_data['price']:8.2f} | Age: {age_min:.1f}min")
                    return cached_data
            
            print(f"❌ CACHE MISS | {symbol:6} | Data too old or not found")
            return None
            
        except Exception as e:
            print(f"⚠️  CACHE ERROR| {symbol:6} | {str(e)}")
            return None
    
    async def _store_price_data(self, symbol: str, price_data: Dict[str, Any]):
        """Store price data in collaborative database cache with validation"""
        try:
            # Validate data before storing
            if not price_data.get('price') or price_data['price'] <= 0:
                print(f"⚠️  INVALID DATA| {symbol:6} | Price: {price_data.get('price', 'N/A')}")
                return
            
            await self.db_service.store_market_data(symbol, price_data)
            print(f"💾 CACHE STORE| {symbol:6} | ${price_data['price']:8.2f} | Stored successfully")
        except Exception as e:
            if "row-level security policy" in str(e):
                print(f"🔒 CACHE SKIP | {symbol:6} | Database permissions issue")
            else:
                print(f"⚠️  CACHE ERROR| {symbol:6} | {str(e)}")
    
    async def _fetch_from_twelvedata(self, symbol: str) -> Dict[str, Any]:
        """Fetch stock quote from Twelve Data API"""
        try:
            if not self.twelvedata_api_key:
                raise Exception("Twelve Data API key not configured")
            
            # Use the quote endpoint for real-time data
            url = f"{self.twelvedata_base_url}/quote"
            params = {
                "symbol": symbol,
                "apikey": self.twelvedata_api_key
            }
            
            print(f"🌐 API CALL   | {symbol:6} | Fetching from Twelve Data...")
            
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            
            # Check for API errors
            if "status" in data and data["status"] == "error":
                error_msg = data.get("message", "Unknown error")
                print(f"❌ API ERROR  | {symbol:6} | {error_msg}")
                raise Exception(f"API Error: {error_msg}")
            
            # Check if we have the required fields
            if "symbol" not in data or "close" not in data:
                print(f"❌ API ERROR  | {symbol:6} | Invalid response format")
                raise Exception("Invalid API response format")
            
            # Parse the response
            price = float(data.get("close", 0))
            previous_close = float(data.get("previous_close", price))
            change = price - previous_close
            change_percent = (change / previous_close * 100) if previous_close != 0 else 0
            
            result = {
                "symbol": symbol.upper(),
                "price": price,
                "change": change,
                "change_percent": change_percent,
                "volume": data.get("volume"),
                "open_price": data.get("open"),
                "high_price": data.get("high"),
                "low_price": data.get("low"),
                "close_price": data.get("previous_close"),
                "cached": False,
                "timestamp": datetime.now().isoformat(),
                "source": "twelvedata",
                "api_key_used": "twelvedata"
            }
            
            change_str = f"{change:+.2f} ({change_percent:+.2f}%)"
            print(f"✅ API SUCCESS| {symbol:6} | ${price:8.2f} | {change_str}")
            return result
            
        except requests.exceptions.RequestException as e:
            print(f"❌ NETWORK ERR| {symbol:6} | {str(e)}")
            raise Exception(f"Network error: {str(e)}")
        except Exception as e:
            print(f"❌ API ERROR  | {symbol:6} | {str(e)}")
            raise Exception(f"API fetch error: {str(e)}")
    
    async def get_stock_quote(self, symbol: str) -> Dict[str, Any]:
        """Get current stock quote with collaborative caching"""
        try:
            print(f"\n📊 QUOTE REQ  | {symbol:6} | Starting price lookup...")
            
            # Check cache first
            cached_data = await self._get_cached_price(symbol)
            if cached_data:
                return cached_data
            
            # Try Twelve Data API
            if self.twelvedata_api_key:
                try:
                    quote_data = await self._fetch_from_twelvedata(symbol)
                    if quote_data and quote_data.get('price'):
                        # Store in collaborative cache
                        await self._store_price_data(symbol, quote_data)
                        return quote_data
                except Exception as e:
                    print(f"❌ FETCH FAIL | {symbol:6} | {str(e)}")
                    raise Exception(f"Failed to fetch data for {symbol}: {str(e)}")
            else:
                raise Exception("Twelve Data API key not configured")
            
        except Exception as e:
            print(f"❌ QUOTE FAIL | {symbol:6} | {str(e)}")
            raise Exception(f"Unable to fetch quote for {symbol}: {str(e)}")
    
    async def get_multiple_quotes(self, symbols: list) -> Dict[str, float]:
        """Get quotes for multiple symbols using collaborative cache"""
        print(f"\n📊 BATCH REQ  | Fetching {len(symbols)} symbols: {', '.join(symbols)}")
        quotes = {}
        
        # First, try to get as many as possible from cache
        cached_prices = await self.db_service.get_cached_prices(symbols)
        
        cache_hits = 0
        api_calls = 0
        
        for symbol in symbols:
            symbol_upper = symbol.upper()
            if symbol_upper in cached_prices:
                quotes[symbol_upper] = cached_prices[symbol_upper]["price"]
                cache_hits += 1
            else:
                # Need to fetch from API
                try:
                    quote_data = await self.get_stock_quote(symbol)
                    quotes[symbol_upper] = quote_data["price"]
                    api_calls += 1
                except Exception as e:
                    print(f"❌ SKIP       | {symbol:6} | Failed: {str(e)}")
                    continue
        
        print(f"📈 BATCH DONE | Cache: {cache_hits}/{len(symbols)} | API: {api_calls}/{len(symbols)}")
        return quotes
    
    async def get_portfolio_quotes(self, portfolio_symbols: list) -> Dict[str, any]:
        """Get quotes for all symbols in portfolio with metadata"""
        print(f"\n💼 PORTFOLIO  | Fetching quotes for {len(portfolio_symbols)} holdings")
        quotes = {}
        
        success_count = 0
        for symbol in portfolio_symbols:
            try:
                quotes[symbol.upper()] = await self.get_stock_quote(symbol)
                success_count += 1
            except Exception as e:
                print(f"❌ SKIP       | {symbol:6} | Failed: {str(e)}")
                continue
        
        print(f"💼 PORTFOLIO  | Success: {success_count}/{len(portfolio_symbols)} quotes fetched")
        return quotes
    
    async def get_historical_data(self, symbol: str, days: int = 30) -> List[Dict[str, Any]]:
        """Get historical price data from our collaborative cache"""
        try:
            return await self.db_service.get_historical_data(symbol, days)
        except Exception as e:
            logger.error(f"Error getting historical data for {symbol}: {str(e)}")
            return []
    
    async def get_cache_stats(self) -> Dict[str, Any]:
        """Get statistics about our collaborative cache"""
        try:
            return await self.db_service.get_market_data_stats()
        except Exception as e:
            logger.error(f"Error getting cache stats: {str(e)}")
            return {"error": str(e)}
    
    def health_check(self) -> Dict[str, any]:
        """Check if the market data service is working"""
        try:
            return {
                "status": "healthy" if self.twelvedata_api_key else "error",
                "twelvedata_key_configured": bool(self.twelvedata_api_key),
                "database_connected": bool(self.db_service),
                "last_test": datetime.now().isoformat(),
                "cache_type": "collaborative_database",
                "api_source": "Twelve Data (800 requests/day)"
            }
        except Exception as e:
            return {
                "status": "error",
                "twelvedata_key_configured": bool(self.twelvedata_api_key),
                "database_connected": bool(self.db_service),
                "last_test": datetime.now().isoformat(),
                "error": str(e)
            }
    
    async def _search_twelvedata(self, query: str) -> List[Dict[str, Any]]:
        """Search stocks using Twelve Data API"""
        try:
            url = f"{self.twelvedata_base_url}/symbol_search"
            params = {
                "symbol": query,
                "apikey": self.twelvedata_api_key
            }
            
            logger.info(f"Searching stocks with Twelve Data for query: {query}")
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=10) as response:
                    response.raise_for_status()
                    data = await response.json()
            
            logger.info(f"Twelve Data search response: {data}")
            
            # Check for API errors
            if "status" in data and data["status"] == "error":
                error_msg = data.get("message", "Unknown error")
                logger.error(f"Twelve Data Search API Error: {error_msg}")
                raise Exception(f"API Error: {error_msg}")
            
            # Parse search results
            results = []
            search_data = data.get("data", [])
            
            for item in search_data[:10]:  # Limit to top 10 results
                try:
                    results.append({
                        'symbol': item.get('symbol', ''),
                        'name': item.get('instrument_name', ''),
                        'type': item.get('instrument_type', 'Equity'),
                        'region': item.get('country', 'United States'),
                        'currency': item.get('currency', 'USD'),
                        'exchange': item.get('exchange', ''),
                        'match_score': 1.0  # Twelve Data doesn't provide match scores
                    })
                except (ValueError, KeyError) as e:
                    logger.warning(f"Error parsing Twelve Data search result: {e}")
                    continue
            
            logger.info(f"✅ Found {len(results)} search results from Twelve Data for query: {query}")
            return results
            
        except Exception as e:
            logger.error(f"Twelve Data search error: {e}")
            raise Exception(f"Search error: {str(e)}")

    async def search_stocks(self, query: str) -> List[Dict[str, Any]]:
        """Search for stocks using Twelve Data"""
        try:
            if not query or len(query.strip()) < 1:
                return []
            
            # Clean the query
            query = query.strip()
            
            # Use Twelve Data search
            if self.twelvedata_api_key:
                try:
                    logger.info(f"Searching stocks with Twelve Data for: {query}")
                    results = await self._search_twelvedata(query)
                    if results:
                        logger.info(f"✅ Successfully got {len(results)} search results from Twelve Data")
                        return results
                    else:
                        logger.warning(f"No search results found for: {query}")
                        return []
                except Exception as e:
                    logger.error(f"❌ Twelve Data search failed: {str(e)}")
                    raise Exception(f"Search failed for '{query}': {str(e)}")
            else:
                raise Exception("Twelve Data API key not configured")
            
        except Exception as e:
            logger.error(f"Error searching stocks: {e}")
            raise Exception(f"Stock search failed: {str(e)}")

    async def get_multiple_quotes_optimized(self, symbols: list) -> Dict[str, Dict[str, Any]]:
        """Get quotes for multiple symbols with optimized caching and batch API calls"""
        if not symbols:
            return {}
        
        quotes = {}
        symbols_to_fetch = []
        
        # First, get as many as possible from cache in one batch query
        logger.info(f"Checking cache for {len(symbols)} symbols...")
        cached_prices = await self.db_service.get_cached_prices(symbols)
        
        # Separate fresh vs stale data
        fresh_symbols = []
        stale_symbols = []
        
        for symbol in symbols:
            symbol_upper = symbol.upper()
            cached_data = cached_prices.get(symbol_upper)
            
            if cached_data and cached_data.get('is_fresh', False):
                quotes[symbol_upper] = cached_data
                fresh_symbols.append(symbol_upper)
            else:
                stale_symbols.append(symbol_upper)
        
        logger.info(f"Cache results: {len(fresh_symbols)} fresh, {len(stale_symbols)} need refresh")
        
        # Fetch stale data from API (could be optimized with batch API calls in future)
        for symbol in stale_symbols:
            try:
                quote_data = await self.get_stock_quote(symbol)
                quotes[symbol] = quote_data
            except Exception as e:
                logger.error(f"Failed to get quote for {symbol}: {str(e)}")
                # Don't include failed symbols in results
                continue
        
        return quotes

    async def warm_cache(self, symbols: List[str]) -> Dict[str, Any]:
        """Warm the cache by pre-fetching data for commonly used symbols"""
        try:
            logger.info(f"Warming cache for {len(symbols)} symbols...")
            
            results = {
                'success': [],
                'failed': [],
                'skipped': []
            }
            
            for symbol in symbols:
                try:
                    # Check if data is already fresh
                    is_fresh = await self.db_service.is_price_data_fresh(symbol, max_age_minutes=5)
                    
                    if is_fresh:
                        results['skipped'].append(symbol)
                        continue
                    
                    # Fetch fresh data
                    quote_data = await self._fetch_from_twelvedata(symbol)
                    if quote_data and quote_data.get('price'):
                        await self._store_price_data(symbol, quote_data)
                        results['success'].append(symbol)
                    else:
                        results['failed'].append(symbol)
                        
                except Exception as e:
                    logger.error(f"Error warming cache for {symbol}: {str(e)}")
                    results['failed'].append(symbol)
            
            logger.info(f"Cache warming complete: {len(results['success'])} success, {len(results['failed'])} failed, {len(results['skipped'])} skipped")
            return results
            
        except Exception as e:
            logger.error(f"Error during cache warming: {str(e)}")
            return {'error': str(e)}

    async def get_cache_performance_metrics(self) -> Dict[str, Any]:
        """Get detailed cache performance metrics"""
        try:
            stats = await self.db_service.get_market_data_stats()
            
            # Calculate additional performance metrics
            total_symbols = stats.get('unique_symbols', 0)
            fresh_symbols = stats.get('freshness', {}).get('fresh_symbols', 0)
            
            cache_hit_rate = (fresh_symbols / total_symbols * 100) if total_symbols > 0 else 0
            
            # Estimate API cost savings
            total_records = stats.get('total_records', 0)
            estimated_api_calls_saved = max(0, total_records - total_symbols)  # Rough estimate
            
            # Performance classification
            if cache_hit_rate >= 80:
                performance_grade = "A"
                performance_desc = "Excellent cache performance"
            elif cache_hit_rate >= 60:
                performance_grade = "B"
                performance_desc = "Good cache performance"
            elif cache_hit_rate >= 40:
                performance_grade = "C"
                performance_desc = "Fair cache performance"
            else:
                performance_grade = "D"
                performance_desc = "Poor cache performance - needs optimization"
            
            return {
                **stats,
                'performance_metrics': {
                    'cache_hit_rate': round(cache_hit_rate, 1),
                    'performance_grade': performance_grade,
                    'performance_description': performance_desc,
                    'estimated_api_calls_saved': estimated_api_calls_saved,
                    'cost_efficiency': 'high' if cache_hit_rate > 70 else 'medium' if cache_hit_rate > 40 else 'low'
                }
            }
            
        except Exception as e:
            logger.error(f"Error getting cache performance metrics: {str(e)}")
            return {'error': str(e)} 