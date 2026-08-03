# CoinMarketCap API Documentation

CoinMarketCap Cryptocurrency API Documentation

[CoinMarketCap Cryptocurrency API Documentation](https://coinmarketcap.com/api/documentation/pro-api-reference)

# Cryptocurrency[](#description)

##### Endpoints for cryptocurrency data. This category includes 19 endpoints:

-   [/v1/cryptocurrency/map](https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency#cryptocurrency-id-map) - Cryptocurrency ID Map
-   [/v3/cryptocurrency/listings/latest](https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency#listings-latest) - Listings Latest
-   [/v1/cryptocurrency/listings/new](https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency#listings-new) - Listings New
-   [/v1/cryptocurrency/listings/historical](https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency#listings-historical) - Listings Historical
-   [/v3/cryptocurrency/quotes/latest](https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency#quotes-latest) - Quotes Latest
-   [/v3/cryptocurrency/quotes/historical](https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency#quotes-historical) - Quotes Historical
-   [/v2/cryptocurrency/ohlcv/latest](https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency#ohlcv-latest) - OHLCV Latest
-   [/v2/cryptocurrency/ohlcv/historical](https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency#ohlcv-historical) - OHLCV Historical
-   [/v2/cryptocurrency/market-pairs/latest](https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency#market-pairs-latest) - Market Pairs Latest
-   [/v2/cryptocurrency/price-performance-stats/latest](https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency#price-performance-stats) - Price Performance Stats
-   [/v2/cryptocurrency/info](https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency#metadata) - Metadata
-   [/v1/cryptocurrency/trending/latest](https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency#trending-latest) - Trending Latest
-   [/v1/cryptocurrency/trending/gainers-losers](https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency#trending-gainers-losers) - Trending Gainers & Losers
-   [/v1/cryptocurrency/trending/most-visited](https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency#trending-most-visited) - Trending Most Visited
-   [/v1/simple/price](https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency#simple-price) - Simple Price
-   [/v1/cryptocurrency/categories](https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency#categories) - Categories
-   [/v1/cryptocurrency/category](https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency#category) - Category
-   [/v1/cryptocurrency/airdrops](https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency#airdrops) - Airdrops
-   [/v1/cryptocurrency/airdrop](https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency#airdrop) - Airdrop

* * *

## Cryptocurrency ID Map[](#cryptocurrency-id-map)

GET

https://pro-api.coinmarketcap.com

/v1/cryptocurrency/map

Returns a mapping of all cryptocurrencies to unique CoinMarketCap `id`s. Per our [Best practices](https://coinmarketcap.com/api/documentation/guides/best-practices) we recommend utilizing CMC ID instead of cryptocurrency symbols to securely identify cryptocurrencies with our other endpoints and in your own application logic. Each cryptocurrency returned includes typical identifiers such as `name`, `symbol`, and `token_address` for flexible mapping to `id`.

By default this endpoint returns cryptocurrencies that have actively tracked markets on supported exchanges. You may receive a map of all inactive cryptocurrencies by passing `listing_status=inactive`. You may also receive a map of registered cryptocurrency projects that are listed but do not yet meet methodology requirements to have tracked markets via `listing_status=untracked`. Please review our [methodology documentation](https://coinmarketcap.com/methodology/) for additional details on listing states.

Cryptocurrencies returned include `first_historical_data` and `last_historical_data` timestamps to conveniently reference historical date ranges available to query with historical time-series data endpoints. You may also use the `aux` parameter to only include properties you require to slim down the payload if calling this endpoint frequently.

**This endpoint is available on the following [API plans](https://coinmarketcap.com/api/pricing/):**

-   Basic
-   Builder
-   Startup
-   Growth
-   Professional
-   Enterprise

**No credit is needed when querying this endpoint.**

Available with no API key

Call this endpoint keyless - no API key, no signup. Prefix the path with `/public-api`: `https://pro-api.coinmarketcap.com/public-api/v1/cryptocurrency/map`. See the [Keyless Public API](https://coinmarketcap.com/api/documentation/pro-api-reference/keyless-public-api) for the full list, rate limits, and examples.

**Cache / Update frequency:** Mapping data is updated only as needed, every 30 seconds.  
**CMC equivalent pages:** No equivalent, this data is only available via API.

### Cryptocurrency ID Map › query Parameters[](#cryptocurrency-id-map/query-parameters)

`listing_status`

​string · pattern: `^(active|inactive|un…`

Only active cryptocurrencies are returned by default. Pass `inactive` to get a list of cryptocurrencies that are no longer active. Pass `untracked` to get a list of cryptocurrencies that are listed but do not yet meet methodology requirements to have tracked markets available. You may pass one or more comma-separated values.

Default: active

`start`

​integer · min: 1

Optionally offset the start (1-based index) of the paginated list of items to return.

Default: 1

`limit`

​integer · min: 1 · max: 5000

Optionally specify the number of results to return. Use this parameter and the "start" parameter to determine your own pagination size.

`sort`

​string · enum

What field to sort the list of cryptocurrencies by.

Enum values:

cmc\_rank

id

Default: id

`symbol`

​string · pattern: `^[0-9A-Za-z$@\-,]+(?…`

Optionally pass a comma-separated list of cryptocurrency symbols to return CoinMarketCap IDs for. If this option is passed, other options will be ignored.

`aux`

​string · pattern: `^(platform|first_his…`

Optionally specify a comma-separated list of supplemental data fields to return. Pass `platform,first_historical_data,last_historical_data,is_active,status` to include all auxiliary fields.

Default: platform,first\_historical\_data,last\_historical\_data,is\_active

### Cryptocurrency ID Map › Responses[](#cryptocurrency-id-map/responses)

200400401403429500

Successful

[Cryptocurrency\_Map\_-\_Response\_Model](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptocurrency-map-response-model)

`data`

​[Cryptocurrency\_Map\_-\_Cryotocurrency\_Object\[\]](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptocurrency-map-cryotocurrency-object) · required

Array of cryptocurrency object results.

`status`

​[API\_Status\_Object](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#api-status-object)

Standardized status object for API calls.

GET/v1/cryptocurrency/map

`curl --request GET \ --url https://pro-api.coinmarketcap.com/v1/cryptocurrency/map`

cURLJavaScriptPythonJavaGoC#KotlinObjective-CPHPRubySwift

Example Responses

200400401403429500

`{ "data": [ { "id": 1, "rank": 1, "name": "Bitcoin", "symbol": "BTC", "slug": "bitcoin", "is_active": 1, "first_historical_data": "2013-04-28T18:47:21.000Z", "last_historical_data": "2020-05-05T20:44:01.000Z", "platform": null }, { "id": 1839, "rank": 3, "name": "Binance Coin", "symbol": "BNB", "slug": "binance-coin", "is_active": 1, "first_historical_data": "2017-07-25T04:30:05.000Z", "last_historical_data": "2020-05-05T20:44:02.000Z", "platform": { "id": 1027, "name": "Ethereum", "symbol": "ETH", "slug": "ethereum", "token_address": "0xB8c77482e45F1F44dE1745F52C74426C631bDD52" } }, { "id": 825, "rank": 5, "name": "Tether", "symbol": "USDT", "slug": "tether", "is_active": 1, "first_historical_data": "2015-02-25T13:34:26.000Z", "last_historical_data": "2020-05-05T20:44:01.000Z", "platform": { "id": 1027, "name": "Ethereum", "symbol": "ETH", "slug": "ethereum", "token_address": "0xdac17f958d2ee523a2206206994597c13d831ec7" } } ], "status": { "timestamp": "2018-06-02T22:51:28.209Z", "error_code": 0, "error_message": "", "elapsed": 10, "credit_count": 1 } }`

json

application/json

* * *

## Listings Latest[](#listings-latest)

GET

https://pro-api.coinmarketcap.com

/v3/cryptocurrency/listings/latest

Returns a paginated list of all active cryptocurrencies with latest market data. The default "market\_cap" sort returns cryptocurrency in order of CoinMarketCap's market cap rank (as outlined in [our methodology](https://coinmarketcap.com/methodology/)) but you may configure this call to order by another market ranking field. Use the "convert" option to return market values in multiple fiat and cryptocurrency conversions in the same call.

You may sort against any of the following:  
**market\_cap**: CoinMarketCap's market cap rank as outlined in [our methodology](https://coinmarketcap.com/methodology/).  
**market\_cap\_strict**: A strict market cap sort (latest trade price x circulating supply).  
**name**: The cryptocurrency name.  
**symbol**: The cryptocurrency symbol.  
**date\_added**: Date cryptocurrency was added to the system.  
**price**: latest average trade price across markets.  
**circulating\_supply**: approximate number of coins currently in circulation.  
**total\_supply**: approximate total amount of coins in existence right now (minus any coins that have been verifiably burned).  
**max\_supply**: our best approximation of the maximum amount of coins that will ever exist in the lifetime of the currency.  
**num\_market\_pairs**: number of market pairs across all exchanges trading each currency.  
**market\_cap\_by\_total\_supply\_strict**: market cap by total supply.  
**volume\_24h**: rolling 24 hour adjusted trading volume.  
**volume\_7d**: rolling 24 hour adjusted trading volume.  
**volume\_30d**: rolling 24 hour adjusted trading volume.  
**percent\_change\_1h**: 1 hour trading price percentage change for each currency.  
**percent\_change\_24h**: 24 hour trading price percentage change for each currency.  
**percent\_change\_7d**: 7 day trading price percentage change for each currency.

**This endpoint is available on the following [API plans](https://coinmarketcap.com/api/pricing/):**

-   Basic
-   Builder
-   Startup
-   Growth
-   Professional
-   Enterprise

Available with no API key

Call this endpoint keyless - no API key, no signup. Prefix the path with `/public-api`: `https://pro-api.coinmarketcap.com/public-api/v3/cryptocurrency/listings/latest`. See the [Keyless Public API](https://coinmarketcap.com/api/documentation/pro-api-reference/keyless-public-api) for the full list, rate limits, and examples.

**Cache / Update frequency:** Every 60 seconds.  
**Plan credit use:** 1 call credit per 200 cryptocurrencies returned (rounded up) and 1 call credit per \`convert\` option beyond the first. **CMC equivalent pages:** Our latest cryptocurrency listing and ranking pages like [coinmarketcap.com/all/views/all/](https://coinmarketcap.com/all/views/all/), [coinmarketcap.com/tokens/](https://coinmarketcap.com/tokens/), [coinmarketcap.com/gainers-losers/](https://coinmarketcap.com/gainers-losers/), [coinmarketcap.com/new/](https://coinmarketcap.com/new/).

_**NOTE:** Use this endpoint if you need a sorted and paginated list of all cryptocurrencies. If you want to query for market data on a few specific cryptocurrencies use `/v3/cryptocurrency/quotes/latest` which is optimized for that purpose. The response data between these endpoints is otherwise the same._

### Listings Latest › query Parameters[](#listings-latest/query-parameters)

`start`

​string

Optionally offset the start (1-based index) of the paginated list of items to return.

Default: 1

`limit`

​string

Optionally specify the number of results to return. Use this parameter and the "start" parameter to determine your own pagination size.

Default: 100

`price_min`

​string

Optionally specify a threshold of minimum USD price to filter results by.

`price_max`

​string

Optionally specify a threshold of maximum USD price to filter results by.

`market_cap_min`

​string

Optionally specify a threshold of minimum market cap to filter results by.

`market_cap_max`

​string

Optionally specify a threshold of maximum market cap to filter results by.

`volume_24h_min`

​string

Optionally specify a threshold of minimum 24 hour USD volume to filter results by.

`volume_24h_max`

​string

Optionally specify a threshold of maximum 24 hour USD volume to filter results by.

`circulating_supply_min`

​string

Optionally specify a threshold of minimum circulating supply to filter results by.

`circulating_supply_max`

​string

Optionally specify a threshold of maximum circulating supply to filter results by.

`percent_change_24h_min`

​string

Optionally specify a threshold of minimum 24 hour percent change to filter results by.

`percent_change_24h_max`

​string

Optionally specify a threshold of maximum 24 hour percent change to filter results by.

`convert`

​string

Optionally calculate market quotes in up to 120 currencies at once by passing a comma-separated list of cryptocurrency or fiat currency symbols. Each additional convert option beyond the first requires an additional call credit. Each conversion is returned in its own "quote" object.

`convert_id`

​string

Optionally calculate market quotes by CoinMarketCap ID instead of symbol. This option is identical to `convert` outside of ID format. Ex: convert\_id=1,2781 would replace convert=BTC,USD in your query. This parameter cannot be used when `convert` is used.

`sort`

​string

What field to sort the list of cryptocurrencies by.

Default: market\_cap

`sort_dir`

​string

The direction in which to order cryptocurrencies against the specified sort.

Default: desc

`cryptocurrency_type`

​string

The type of cryptocurrency to include.

Default: all

`tag`

​string

The tag of cryptocurrency to include.

Default: all

`aux`

​string

Optionally specify a comma-separated list of supplemental data fields to return. Pass num\_market\_pairs,cmc\_rank,date\_added,tags,platform,max\_supply,circulating\_supply,total\_supply,market\_cap\_by\_total\_supply,volume\_24h\_reported,volume\_7d,volume\_7d\_reported,volume\_30d,volume\_30d\_reported,is\_market\_cap\_included\_in\_calc to include all auxiliary fields.

Default: num\_market\_pairs,cmc\_rank,date\_added,tags,platform,max\_supply,circulating\_supply,total\_supply

### Listings Latest › Headers[](#listings-latest/header-parameters)

`X-CMC_PRO_API_KEY`

​string · required

Your CoinMarketCap Pro API key

Default: YOUR\_API\_KEY

### Listings Latest › Responses[](#listings-latest/responses)

200400401403429500

OK

`data`

​[CryptoListingDTO\[\]](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptolistingdto) · required

`status`

​[API\_Status\_Object](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#api-status-object)

Standardized status object for API calls.

GET/v3/cryptocurrency/listings/latest

`curl --request GET \ --url https://pro-api.coinmarketcap.com/v3/cryptocurrency/listings/latest \ --header 'X-CMC_PRO_API_KEY: YOUR_API_KEY'`

cURLJavaScriptPythonJavaGoC#KotlinObjective-CPHPRubySwift

Example Responses

200400401403429500

`{ "data": [ { "tags": [ "mineable" ], "id": 1, "name": "Bitcoin", "symbol": "BTC", "slug": "bitcoin", "infinite_supply": false, "circulating_supply": 20044984, "total_supply": 20044984, "max_supply": 21000000, "date_added": "2010-07-13T00:00:00.000Z", "num_market_pairs": 12660, "cmc_rank": 1, "last_updated": "2026-06-19T14:02:00.000Z", "tvl_ratio": null, "platform": null, "self_reported_circulating_supply": null, "self_reported_market_cap": null, "minted_market_cap": 1265258535378.41, "quote": [ { "id": 2781, "symbol": "USD", "price": 63120.95511667226, "volume_24h": 29127614493.574356, "volume_change_24h": -8.4273, "cex_volume_24h": 29080164577.906445, "dex_volume_24h": 47449915.66790789, "percent_change_1h": 0.61500999, "percent_change_24h": -1.19783194, "percent_change_7d": -0.28271701, "percent_change_30d": -18.14292343, "percent_change_60d": -16.03754846, "percent_change_90d": -10.78181948, "market_cap": 1265258535378.4136, "market_cap_dominance": 58.2494, "fully_diluted_market_cap": 1325540057450.12, "minted_market_cap": 1265258535378.41, "tvl": null, "last_updated": "2026-06-19T14:02:00.000Z" } ] }, { "tags": [ "stablecoin" ], "id": 825, "name": "Tether USDt", "symbol": "USDT", "slug": "tether", "infinite_supply": true, "circulating_supply": 186471454928.25275, "total_supply": 193153431960.50647, "max_supply": null, "date_added": "2015-02-25T00:00:00.000Z", "num_market_pairs": 186448, "cmc_rank": 3, "last_updated": "2026-06-19T14:01:00.000Z", "tvl_ratio": null, "platform": { "id": 1027, "slug": "ethereum", "name": "Ethereum", "symbol": "ETH", "token_address": "0xdac17f958d2ee523a2206206994597c13d831ec7" }, "self_reported_circulating_supply": null, "self_reported_market_cap": null, "minted_market_cap": 192954912191.18, "quote": [ { "id": 2781, "symbol": "USD", "price": 0.9989722172300717, "volume_24h": 62071695050.04424, "volume_change_24h": -12.0707, "cex_volume_24h": 60510731381.50478, "dex_volume_24h": 1560963668.5394013, "percent_change_1h": -0.0150212, "percent_change_24h": 0.02100214, "percent_change_7d": 0.00457887, "percent_change_30d": -0.00471566, "percent_change_60d": -0.14441328, "percent_change_90d": -0.08226185, "market_cap": 186279802779.794, "market_cap_dominance": 8.5763, "fully_diluted_market_cap": 192954912191.18, "minted_market_cap": 192954912191.18, "tvl": null, "last_updated": "2026-06-19T14:01:00.000Z" } ] }, { "tags": [ "stablecoin" ], "id": 3408, "name": "USDC", "symbol": "USDC", "slug": "usd-coin", "infinite_supply": false, "circulating_supply": 74934715491.34196, "total_supply": 74934715491.34196, "max_supply": null, "date_added": "2018-10-08T00:00:00.000Z", "num_market_pairs": 40513, "cmc_rank": 5, "last_updated": "2026-06-19T14:01:00.000Z", "tvl_ratio": null, "platform": { "id": 1027, "slug": "ethereum", "name": "Ethereum", "symbol": "ETH", "token_address": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48" }, "self_reported_circulating_supply": 60901219650.23, "self_reported_market_cap": 60891043102.079185, "minted_market_cap": 74922193956.56, "quote": [ { "id": 2781, "symbol": "USD", "price": 0.9998329007496195, "volume_24h": 9375210843.24513, "volume_change_24h": -22.0589, "cex_volume_24h": 7294870263.140532, "dex_volume_24h": 2080340580.104594, "percent_change_1h": 0.00101421, "percent_change_24h": 0.01332693, "percent_change_7d": 0.00411852, "percent_change_30d": 0.0208214, "percent_change_60d": -0.00167445, "percent_change_90d": -0.00818018, "market_cap": 74922193956.55588, "market_cap_dominance": 3.4492, "fully_diluted_market_cap": 74922193956.56, "minted_market_cap": 74922193956.56, "tvl": null, "last_updated": "2026-06-19T14:01:00.000Z" } ] }, { "tags": [ "ethereum-ecosystem" ], "id": 5176, "name": "Tether Gold", "symbol": "XAUt", "slug": "tether-gold", "infinite_supply": false, "circulating_supply": 612823.659532, "total_supply": 707747.089, "max_supply": null, "date_added": "2020-02-07T00:00:00.000Z", "num_market_pairs": 401, "cmc_rank": 33, "last_updated": "2026-06-19T14:01:00.000Z", "tvl_ratio": 0.85231497, "platform": { "id": 1027, "slug": "ethereum", "name": "Ethereum", "symbol": "ETH", "token_address": "0x68749665FF8D2d112Fa859AA293F07A622782F38" }, "self_reported_circulating_supply": 48450, "self_reported_market_cap": 200583379.1418529, "minted_market_cap": 2930078486.88, "quote": [ { "id": 2781, "symbol": "USD", "price": 4140.00782542524, "volume_24h": 192201727.25124744, "volume_change_24h": -34.2878, "cex_volume_24h": 181966210.11893946, "dex_volume_24h": 10235517.13230798, "percent_change_1h": -0.57455114, "percent_change_24h": -2.25096351, "percent_change_7d": -0.78415585, "percent_change_30d": -7.59755401, "percent_change_60d": -13.72518949, "percent_change_90d": -7.84994801, "market_cap": 2537094746.068213, "market_cap_dominance": 0.1168, "fully_diluted_market_cap": 2930078486.88, "minted_market_cap": 2930078486.88, "tvl": 2976710281, "last_updated": "2026-06-19T14:01:00.000Z" } ] } ], "status": { "timestamp": "2026-06-19T14:03:33.664Z", "error_code": 0, "error_message": "", "elapsed": 8, "credit_count": 1 } }`

json

application/json

* * *

## Listings New[](#listings-new)

GET

https://pro-api.coinmarketcap.com

/v1/cryptocurrency/listings/new

Returns a paginated list of most recently added cryptocurrencies.

**This endpoint is available on the following [API plans](https://coinmarketcap.com/api/pricing/):**

-   Startup
-   Growth
-   Professional
-   Enterprise

**Cache / Update frequency:** Every 60 seconds.  
**Plan credit use:** 1 call credit per 200 cryptocurrencies returned (rounded up) and 1 call credit per `convert` option beyond the first.  
**CMC equivalent pages:** Our "new" cryptocurrency page [coinmarketcap.com/new/](https://coinmarketcap.com/new)

_**NOTE:** Use this endpoint if you need a sorted and paginated list of all recently added cryptocurrencies._

### Listings New › query Parameters[](#listings-new/query-parameters)

`start`

​integer · min: 1

Optionally offset the start (1-based index) of the paginated list of items to return.

Default: 1

`limit`

​integer · min: 1 · max: 5000

Optionally specify the number of results to return. Use this parameter and the "start" parameter to determine your own pagination size.

Default: 100

`convert`

​string · pattern: `^[0-9A-Za-z$@\-,]+(?…`

Optionally calculate market quotes in up to 120 currencies at once by passing a comma-separated list of cryptocurrency or fiat currency symbols. Each additional convert option beyond the first requires an additional call credit. A list of supported fiat options can be found [here](https://coinmarketcap.com/api/documentation/guides/standards-and-conventions). Each conversion is returned in its own "quote" object.

`convert_id`

​string · pattern: `^\d+(?:,\d+)*$`

Optionally calculate market quotes by CoinMarketCap ID instead of symbol. This option is identical to `convert` outside of ID format. Ex: convert\_id=1,2781 would replace convert=BTC,USD in your query. This parameter cannot be used when `convert` is used.

`sort_dir`

​string · enum

The direction in which to order cryptocurrencies against the specified sort.

Enum values:

asc

desc

### Listings New › Headers[](#listings-new/header-parameters)

`X-CMC_PRO_API_KEY`

​string · required

Your CoinMarketCap Pro API key

Default: YOUR\_API\_KEY

### Listings New › Responses[](#listings-new/responses)

200400401403429500

Successful

[Cryptocurrency\_Listings\_New\_-\_Response\_Model](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptocurrency-listings-new-response-model)

`data`

​[Cryptocurrency\_Listings\_Latest\_-\_Cryptocurrency\_object\_2\[\]](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptocurrency-listings-latest-cryptocurrency-object-2) · required

Array of cryptocurrency objects matching the list options.

`status`

​[API\_Status\_Object](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#api-status-object)

Standardized status object for API calls.

GET/v1/cryptocurrency/listings/new

`curl --request GET \ --url https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/new \ --header 'X-CMC_PRO_API_KEY: YOUR_API_KEY'`

cURLJavaScriptPythonJavaGoC#KotlinObjective-CPHPRubySwift

Example Responses

200400401403429500

`{ "data": [ { "id": 1, "name": "Bitcoin", "symbol": "BTC", "slug": "bitcoin", "cmc_rank": 5, "num_market_pairs": 500, "circulating_supply": 16950100, "total_supply": 16950100, "max_supply": 21000000, "last_updated": "2018-06-02T22:51:28.209Z", "date_added": "2013-04-28T00:00:00.000Z", "tags": [ "mineable" ], "platform": null, "minted_market_cap": 1802955697670.94, "quote": { "USD": { "price": 9283.92, "volume_24h": 7155680000, "volume_change_24h": -0.152774, "percent_change_1h": -0.152774, "percent_change_24h": 0.518894, "percent_change_7d": 0.986573, "market_cap": 852164659250.2758, "market_cap_dominance": 51, "fully_diluted_market_cap": 952835089431.14, "last_updated": "2018-08-09T22:53:32.000Z" } } }, { "id": 1027, "name": "Ethereum", "symbol": "ETH", "slug": "ethereum", "num_market_pairs": 6360, "circulating_supply": 16950100, "total_supply": 16950100, "max_supply": 21000000, "last_updated": "2018-06-02T22:51:28.209Z", "date_added": "2013-04-28T00:00:00.000Z", "tags": [ "mineable" ], "platform": null, "minted_market_cap": 364793475572.53, "quote": { "USD": { "price": 1283.92, "volume_24h": 7155680000, "volume_change_24h": -0.152774, "percent_change_1h": -0.152774, "percent_change_24h": 0.518894, "percent_change_7d": 0.986573, "market_cap": 158055024432, "market_cap_dominance": 51, "fully_diluted_market_cap": 952835089431.14, "last_updated": "2018-08-09T22:53:32.000Z" } } } ], "status": { "timestamp": "2018-06-02T22:51:28.209Z", "error_code": 0, "error_message": "", "elapsed": 10, "credit_count": 1 } }`

json

application/json

* * *

## Listings Historical[](#listings-historical)

GET

https://pro-api.coinmarketcap.com

/v1/cryptocurrency/listings/historical

Returns a ranked and sorted list of all cryptocurrencies for a historical UTC date.

**Technical Notes**

-   This endpoint is identical in format to our `/cryptocurrency/listings/latest` endpoint but is used to retrieve historical daily ranking snapshots from the end of each UTC day.
-   Daily snapshots reflect market data at the end of each UTC day and may be requested as far back as 2013-04-28 (as supported by your plan's historical limits).
-   The required "date" parameter can be passed as a Unix timestamp or ISO 8601 date but only the date portion of the timestamp will be referenced. It is recommended to send an ISO date format like "2019-10-10" without time.
-   This endpoint is for retrieving paginated and sorted lists of all currencies. If you require historical market data on specific cryptocurrencies you should use `/cryptocurrency/quotes/historical`.

Cryptocurrencies are listed by cmc\_rank by default. You may optionally sort against any of the following:  
**cmc\_rank**: CoinMarketCap's market cap rank as outlined in [our methodology](https://coinmarketcap.com/methodology/).  
**name**: The cryptocurrency name.  
**symbol**: The cryptocurrency symbol.  
**market\_cap**: market cap (latest trade price x circulating supply).  
**price**: latest average trade price across markets.  
**circulating\_supply**: approximate number of coins currently in circulation.  
**total\_supply**: approximate total amount of coins in existence right now (minus any coins that have been verifiably burned).  
**max\_supply**: our best approximation of the maximum amount of coins that will ever exist in the lifetime of the currency.  
**num\_market\_pairs**: number of market pairs across all exchanges trading each currency.  
**volume\_24h**: 24 hour trading volume for each currency.  
**percent\_change\_1h**: 1 hour trading price percentage change for each currency.  
**percent\_change\_24h**: 24 hour trading price percentage change for each currency.  
**percent\_change\_7d**: 7 day trading price percentage change for each currency.

**This endpoint is available on the following [API plans](https://coinmarketcap.com/api/pricing/):**

-   Basic (1 year)
-   Builder (3 years)
-   Startup (from 2013)
-   Growth (from 2013)
-   Professional (from 2013)
-   Enterprise (from 2013)

**Cache / Update frequency:** The last completed UTC day is available 30 minutes after midnight on the next UTC day.  
**Plan credit use:** 1 call credit per 100 cryptocurrencies returned (rounded up) and 1 call credit per `convert` option beyond the first.  
**CMC equivalent pages:** Our historical daily crypto ranking snapshot pages like this one on [February 02, 2014](https://coinmarketcap.com/historical/20140202/).

### Listings Historical › query Parameters[](#listings-historical/query-parameters)

`date`

​string · required

date (Unix or ISO 8601) to reference day of snapshot.

`start`

​integer · min: 1

Optionally offset the start (1-based index) of the paginated list of items to return.

Default: 1

`limit`

​integer · min: 1 · max: 5000

Optionally specify the number of results to return. Use this parameter and the "start" parameter to determine your own pagination size.

Default: 100

`convert`

​string · pattern: `^[0-9A-Za-z$@\-,]+(?…`

Optionally calculate market quotes in up to 120 currencies at once by passing a comma-separated list of cryptocurrency or fiat currency symbols. Each additional convert option beyond the first requires an additional call credit. A list of supported fiat options can be found [here](https://coinmarketcap.com/api/documentation/guides/standards-and-conventions). Each conversion is returned in its own "quote" object.

`convert_id`

​string · pattern: `^\d+(?:,\d+)*$`

Optionally calculate market quotes by CoinMarketCap ID instead of symbol. This option is identical to `convert` outside of ID format. Ex: convert\_id=1,2781 would replace convert=BTC,USD in your query. This parameter cannot be used when `convert` is used.

`sort`

​string · enum

What field to sort the list of cryptocurrencies by.

Enum values:

cmc\_rank

name

symbol

market\_cap

price

circulating\_supply

total\_supply

max\_supply

show 5 more

Default: cmc\_rank

`sort_dir`

​string · enum

The direction in which to order cryptocurrencies against the specified sort.

Enum values:

asc

desc

`cryptocurrency_type`

​string · enum

The type of cryptocurrency to include.

Enum values:

all

coins

tokens

Default: all

`aux`

​string · pattern: `^(platform|tags|date…`

Optionally specify a comma-separated list of supplemental data fields to return. Pass `platform,tags,date_added,circulating_supply,total_supply,max_supply,cmc_rank,num_market_pairs` to include all auxiliary fields.

Default: platform,tags,date\_added,circulating\_supply,total\_supply,max\_supply,cmc\_rank,num\_market\_pairs

### Listings Historical › Headers[](#listings-historical/header-parameters)

`X-CMC_PRO_API_KEY`

​string · required

Your CoinMarketCap Pro API key

Default: YOUR\_API\_KEY

### Listings Historical › Responses[](#listings-historical/responses)

200400401403429500

Successful

[Cryptocurrency\_Listings\_Latest\_-\_Response\_Model](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptocurrency-listings-latest-response-model)

`data`

​[Cryptocurrency\_Listings\_Latest\_-\_Cryptocurrency\_object\[\]](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptocurrency-listings-latest-cryptocurrency-object) · required

Array of cryptocurrency objects matching the list options.

`status`

​[API\_Status\_Object](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#api-status-object)

Standardized status object for API calls.

GET/v1/cryptocurrency/listings/historicalTest

`curl --request GET \ --url 'https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/historical?date=%3Cstring%3E' \ --header 'X-CMC_PRO_API_KEY: YOUR_API_KEY'`

cURLJavaScriptPythonJavaGoC#KotlinObjective-CPHPRubySwift

Example Responses

200400401403429500

`{ "data": [ { "id": 1, "name": "Bitcoin", "symbol": "BTC", "slug": "bitcoin", "cmc_rank": 1, "num_market_pairs": 500, "circulating_supply": 17200062, "total_supply": 17200062, "max_supply": 21000000, "last_updated": "2018-06-02T22:51:28.209Z", "date_added": "2013-04-28T00:00:00.000Z", "tags": [ "mineable" ], "platform": null, "quote": { "USD": { "price": 9283.92, "volume_24h": 7155680000, "percent_change_1h": -0.152774, "percent_change_24h": 0.518894, "percent_change_7d": 0.986573, "market_cap": 158055024432, "last_updated": "2018-08-09T22:53:32.000Z" } } }, { "id": 1027, "name": "Ethereum", "symbol": "ETH", "slug": "ethereum", "num_market_pairs": 6089, "circulating_supply": 17200062, "total_supply": 17200062, "max_supply": 21000000, "last_updated": "2018-06-02T22:51:28.209Z", "date_added": "2013-04-28T00:00:00.000Z", "tags": [ "mineable" ], "platform": null, "quote": { "USD": { "price": 1678.6501384942708, "volume_24h": 7155680000, "percent_change_1h": -0.152774, "percent_change_24h": 0.518894, "percent_change_7d": 0.986573, "market_cap": 158055024432, "last_updated": "2018-08-09T22:53:32.000Z" } }, "cmc_rank": 2 } ], "status": { "timestamp": "2019-04-02T22:44:24.200Z", "error_code": 0, "error_message": "", "elapsed": 10, "credit_count": 1 } }`

json

application/json

* * *

## Quotes Latest[](#quotes-latest)

GET

https://pro-api.coinmarketcap.com

/v3/cryptocurrency/quotes/latest

Returns the latest market quote for 1 or more cryptocurrencies. Use the "convert" option to return market values in multiple fiat and cryptocurrency conversions in the same call.

**Please note**: This documentation relates to our updated V3 endpoint, which may be incompatible with our V2 versions. Documentation for deprecated endpoints can be found [the deprecated section](https://coinmarketcap.com/api/documentation/pro-api-reference/deprecated).

**This endpoint is available on the following [API plans](https://coinmarketcap.com/api/pricing/):**

-   Basic
-   Startup
-   Builder
-   Growth
-   Professional
-   Enterprise

Available with no API key

Call this endpoint keyless - no API key, no signup. Prefix the path with `/public-api`: `https://pro-api.coinmarketcap.com/public-api/v3/cryptocurrency/quotes/latest`. See the [Keyless Public API](https://coinmarketcap.com/api/documentation/pro-api-reference/keyless-public-api) for the full list, rate limits, and examples.

**Cache / Update frequency:** Every 60 seconds.  
**Plan credit use:** 1 call credit per 100 cryptocurrencies returned (rounded up) and 1 call credit per \`convert\` option beyond the first.  
**CMC equivalent pages:** Latest market data pages for specific cryptocurrencies like [coinmarketcap.com/currencies/bitcoin/](https://coinmarketcap.com/currencies/bitcoin/).

_**NOTE:** Use this endpoint to request the latest quote for specific cryptocurrencies. If you need to request all cryptocurrencies use `/v3/cryptocurrency/listings/latest` which is optimized for that purpose. The response data between these endpoints is otherwise the same._

### Quotes Latest › query Parameters[](#quotes-latest/query-parameters)

`id`

​string

One or more comma-separated cryptocurrency CoinMarketCap IDs.

`slug`

​string

Alternatively pass a comma-separated list of cryptocurrency slugs.

`symbol`

​string

Alternatively pass one or more comma-separated cryptocurrency symbols.

`convert`

​string

Optionally calculate market quotes in up to 120 currencies at once by passing a comma-separated list of cryptocurrency or fiat currency symbols.

`convert_id`

​string

Optionally calculate market quotes by CoinMarketCap ID instead of symbol.

`aux`

​string

Optionally specify a comma-separated list of supplemental data fields to return.

`skip_invalid`

​string

Pass true to relax request validation rules. When requesting records on multiple cryptocurrencies an error is returned if no match is found for 1 or more requested cryptocurrencies. If set to true, invalid lookups will be skipped allowing valid cryptocurrencies to still be returned.

### Quotes Latest › Headers[](#quotes-latest/header-parameters)

`X-CMC_PRO_API_KEY`

​string · required

Your CoinMarketCap Pro API key

Default: YOUR\_API\_KEY

### Quotes Latest › Responses[](#quotes-latest/responses)

200

OK

​[CryptoQuoteV3DTO\[\]](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptoquotev3dto)

[CryptoQuoteV3DTO](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptoquotev3dto)

`id`

​integer · int32

`name`

​string

`symbol`

​string

`slug`

​string

`platform`

​[Platform](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#platform)

`quote`

​[Quote\[\]](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#quote)

`tags`

​[CryptoTag\[\]](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptotag)

`is_active`

​integer · int32

`infinite_supply`

​boolean

`is_market_cap_included_in_calc`

​integer · int32

`is_fiat`

​integer · int32

`circulating_supply`

​number · bigdecimal

Circulating supply of the cryptocurrency

`total_supply`

​number · bigdecimal

Total supply of the cryptocurrency

`max_supply`

​number · bigdecimal

Maximum supply of the cryptocurrency

`date_added`

​string

`num_market_pairs`

​integer · int32

`cmc_rank`

​integer · int32

`last_updated`

​string

`tvl_ratio`

​number · bigdecimal

TVL to market cap ratio

`self_reported_circulating_supply`

​number · bigdecimal

Self-reported circulating supply

`self_reported_market_cap`

​number · bigdecimal

Self-reported market capitalization

`unlocked_circulating_supply`

​number · bigdecimal

Unlocked circulating supply

`unlocked_market_cap`

​number · bigdecimal

Unlocked market capitalization

GET/v3/cryptocurrency/quotes/latest

`curl --request GET \ --url https://pro-api.coinmarketcap.com/v3/cryptocurrency/quotes/latest \ --header 'X-CMC_PRO_API_KEY: YOUR_API_KEY'`

cURLJavaScriptPythonJavaGoC#KotlinObjective-CPHPRubySwift

Example Responses

200

`[ { "id": 0, "name": "name", "symbol": "symbol", "slug": "slug", "platform": { "id": 0, "slug": "slug", "name": "name", "symbol": "symbol", "token_address": "token_address" }, "quote": [ { "id": 0, "symbol": "symbol", "price": 0, "volume_24h": 0, "cex_volume_24h": 0, "dex_volume_24h": 0, "volume_24h_reported": 0, "volume_7d": 0, "volume_7d_reported": 0, "volume_30d": 0, "volume_30d_reported": 0, "volume_change_24h": 0, "percent_change_1h": 0, "percent_change_24h": 0, "percent_change_7d": 0, "percent_change_30d": 0, "percent_change_60d": 0, "percent_change_90d": 0, "market_cap": 0, "market_cap_dominance": 0, "fully_diluted_market_cap": 0, "minted_market_cap": 0, "tvl": 0, "market_cap_by_total_supply": 0, "last_updated": "last_updated" } ], "tags": [ { "slug": "slug", "name": "name", "category": "category" } ], "is_active": 0, "infinite_supply": true, "is_market_cap_included_in_calc": 0, "is_fiat": 0, "circulating_supply": 0, "total_supply": 0, "max_supply": 0, "date_added": "date_added", "num_market_pairs": 0, "cmc_rank": 0, "last_updated": "last_updated", "tvl_ratio": 0, "self_reported_circulating_supply": 0, "self_reported_market_cap": 0, "unlocked_circulating_supply": 0, "unlocked_market_cap": 0 } ]`

json

application/json

* * *

## Quotes Historical[](#quotes-historical)

GET

https://pro-api.coinmarketcap.com

/v3/cryptocurrency/quotes/historical

Returns an interval of historic market quotes for any cryptocurrency based on time and interval parameters.

**Please note**: This documentation relates to our updated V3 endpoint, which may be incompatible with our V1 versions. Documentation for deprecated endpoints can be found [the deprecated section](https://coinmarketcap.com/api/documentation/pro-api-reference/deprecated).

**Technical Notes**

-   A historic quote for every "interval" period between your "time\_start" and "time\_end" will be returned.
-   If a "time\_start" is not supplied, the "interval" will be applied in reverse from "time\_end".
-   If "time\_end" is not supplied, it defaults to the current time.
-   At each "interval" period, the historic quote that is closest in time to the requested time will be returned.
-   If no historic quotes are available in a given "interval" period up until the next interval period, it will be skipped.

**Implementation Tips**

-   Want to get the last quote of each UTC day? Don't use "interval=daily" as that returns the first quote. Instead use "interval=24h" to repeat a specific timestamp search every 24 hours and pass ex. "time\_start=2019-01-04T23:59:00.000Z" to query for the last record of each UTC day.
-   This endpoint supports requesting multiple cryptocurrencies in the same call. Please note the API response will be wrapped in an additional object in this case.

**Interval Options**  
There are 2 types of time interval formats that may be used for "interval".

The first are calendar year and time constants in UTC time:  
**"hourly"** - Get the first quote available at the beginning of each calendar hour.  
**"daily"** - Get the first quote available at the beginning of each calendar day.  
**"weekly"** - Get the first quote available at the beginning of each calendar week.  
**"monthly"** - Get the first quote available at the beginning of each calendar month.  
**"yearly"** - Get the first quote available at the beginning of each calendar year.

The second are relative time intervals.  
**"m"**: Get the first quote available every "m" minutes (60 second intervals). Supported minutes are: "5m", "10m", "15m", "30m", "45m".  
**"h"**: Get the first quote available every "h" hours (3600 second intervals). Supported hour intervals are: "1h", "2h", "3h", "4h", "6h", "12h".  
**"d"**: Get the first quote available every "d" days (86400 second intervals). Supported day intervals are: "1d", "2d", "3d", "7d", "14d", "15d", "30d", "60d", "90d", "365d".

**This endpoint is available on the following [API plans](https://coinmarketcap.com/api/pricing/):**

-   Basic (intraday: 1 month, daily: 1 year)
-   Builder (intraday: 1 month, daily: 3 years)
-   Startup (intraday: 1 month, daily: from 2010)
-   Growth (intraday: 3 months, daily: from 2010)
-   Professional (intraday: 12 months, daily: from 2010)
-   Enterprise (intraday: from 2010, daily: from 2010)

**Note:** Intraday = `5m-12h` intervals, Daily = `1d-1y` intervals

**Cache / Update frequency:** Every 5 minutes.  
**Plan credit use:** 1 call credit per 100 historical data points returned (rounded up) and 1 call credit per `convert` option beyond the first.  
**CMC equivalent pages:** Our historical cryptocurrency charts like [coinmarketcap.com/currencies/bitcoin/#charts](https://coinmarketcap.com/currencies/bitcoin/#charts).

### Quotes Historical › query Parameters[](#quotes-historical/query-parameters)

`id`

​string · pattern: `^\d+(?:,\d+)*$`

One or more comma-separated CoinMarketCap cryptocurrency IDs. Example: "1,2"

`symbol`

​string · pattern: `^[0-9A-Za-z$@\-,]+(?…`

Alternatively pass one or more comma-separated cryptocurrency symbols. Example: "BTC,ETH". At least one "id" _or_ "symbol" is required for this request.

`time_start`

​string

Timestamp (Unix or ISO 8601) to start returning quotes for. Optional, if not passed, we'll return quotes calculated in reverse from "time\_end".

`time_end`

​string

Timestamp (Unix or ISO 8601) to stop returning quotes for (inclusive). Optional, if not passed, we'll default to the current time. If no "time\_start" is passed, we return quotes in reverse order starting from this time.

`count`

​number · min: 1 · max: 10000

The number of interval periods to return results for. Optional, required if both "time\_start" and "time\_end" aren't supplied. The default is 10 items. The current query limit is 10000.

Default: 10

`interval`

​string · enum

Interval of time to return data points for. See details in endpoint description.

Enum values:

yearly

monthly

weekly

daily

hourly

5m

10m

15m

show 19 more

Default: 5m

`convert`

​string · pattern: `^[0-9A-Za-z$@\-,]+(?…`

By default market quotes are returned in USD. Optionally calculate market quotes in up to 3 other fiat currencies or cryptocurrencies.

`convert_id`

​string · pattern: `(^\d+(?:,\d+)*$|(\d,…`

Optionally calculate market quotes by CoinMarketCap ID instead of symbol. This option is identical to `convert` outside of ID format. Ex: convert\_id=1,2781 would replace convert=BTC,USD in your query. This parameter cannot be used when `convert` is used.

`aux`

​string · pattern: `^(price|volume|marke…`

Optionally specify a comma-separated list of supplemental data fields to return. Pass `price,volume,market_cap,circulating_supply,total_supply,quote_timestamp,is_active,is_fiat,search_interval` to include all auxiliary fields.

Default: price,volume,market\_cap,circulating\_supply,total\_supply,quote\_timestamp,is\_active,is\_fiat

`skip_invalid`

​boolean

Pass `true` to relax request validation rules. When requesting records on multiple cryptocurrencies an error is returned if no match is found for 1 or more requested cryptocurrencies. If set to true, invalid lookups will be skipped allowing valid cryptocurrencies to still be returned.

Default: true

### Quotes Historical › Headers[](#quotes-historical/header-parameters)

`X-CMC_PRO_API_KEY`

​string · required

Your CoinMarketCap Pro API key

Default: YOUR\_API\_KEY

### Quotes Historical › Responses[](#quotes-historical/responses)

200400401403429500

Successful

[Cryptocurrency\_Quotes\_Historical\_-\_Response\_Model](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptocurrency-quotes-historical-response-model)

`data`

​[Cryptocurrency\_Quotes\_Historical\_-\_Results\_map](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptocurrency-quotes-historical-results-map) · required

Results of your query returned as an object map.

`status`

​[API\_Status\_Object](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#api-status-object)

Standardized status object for API calls.

GET/v3/cryptocurrency/quotes/historical

`curl --request GET \ --url https://pro-api.coinmarketcap.com/v3/cryptocurrency/quotes/historical \ --header 'X-CMC_PRO_API_KEY: YOUR_API_KEY'`

cURLJavaScriptPythonJavaGoC#KotlinObjective-CPHPRubySwift

Example Responses

200400401403429500

`{ "data": { "1": { "id": 1, "name": "Bitcoin", "symbol": "BTC", "is_active": 1, "is_fiat": 0, "quotes": [ { "timestamp": "2018-06-22T00:00:00.000Z", "quote": { "USD": { "price": 6242.48, "volume_24h": 4894120000, "market_cap": 107057808682, "last_updated": "2018-06-22T00:04:16.000Z", "volume_24hr": 4894120000, "timestamp": "2018-06-22T00:04:16.000Z" } } } ] } }, "status": { "timestamp": "2026-03-05T22:43:48.471Z", "error_code": 0, "error_message": "", "elapsed": 10, "credit_count": 1, "notice": "" } }`

json

application/json

* * *

## OHLCV Latest[](#ohlcv-latest)

GET

https://pro-api.coinmarketcap.com

/v2/cryptocurrency/ohlcv/latest

Returns the latest OHLCV (Open, High, Low, Close, Volume) market values for one or more cryptocurrencies for the current UTC day. Since the current UTC day is still active these values are updated frequently. You can find the final calculated OHLCV values for the last completed UTC day along with all historic days using /cryptocurrency/ohlcv/historical.

**Please note**: This documentation relates to our updated V2 endpoint, which may be incompatible with our V1 versions. Documentation for deprecated endpoints can be found [the deprecated section](https://coinmarketcap.com/api/documentation/pro-api-reference/deprecated).

**This endpoint is available on the following [API plans](https://coinmarketcap.com/api/pricing/):**

-   ~Basic~
-   ~Builder~
-   Startup
-   Growth
-   Professional
-   Enterprise

**Cache / Update frequency:** Every 10 minutes. Additional OHLCV intervals and 1 minute updates will be available in the future.  
**Plan credit use:** 1 call credit per 100 OHLCV values returned (rounded up) and 1 call credit per `convert` option beyond the first.  
**CMC equivalent pages:** No equivalent, this data is only available via API.

### OHLCV Latest › query Parameters[](#ohlcv-latest/query-parameters)

`id`

​string · pattern: `^\d+(?:,\d+)*$`

One or more comma-separated cryptocurrency CoinMarketCap IDs. Example: 1,2

`symbol`

​string · pattern: `^[0-9A-Za-z$@\-,]+(?…`

Alternatively pass one or more comma-separated cryptocurrency symbols. Example: "BTC,ETH". At least one "id" _or_ "symbol" is required.

`convert`

​string · pattern: `^[0-9A-Za-z$@\-,]+(?…`

Optionally calculate market quotes in up to 120 currencies at once by passing a comma-separated list of cryptocurrency or fiat currency symbols. Each additional convert option beyond the first requires an additional call credit. A list of supported fiat options can be found [here](https://coinmarketcap.com/api/documentation/guides/standards-and-conventions). Each conversion is returned in its own "quote" object.

`convert_id`

​string · pattern: `^\d+(?:,\d+)*$`

Optionally calculate market quotes by CoinMarketCap ID instead of symbol. This option is identical to `convert` outside of ID format. Ex: convert\_id=1,2781 would replace convert=BTC,USD in your query. This parameter cannot be used when `convert` is used.

`skip_invalid`

​boolean

Pass `true` to relax request validation rules. When requesting records on multiple cryptocurrencies an error is returned if any invalid cryptocurrencies are requested or a cryptocurrency does not have matching records in the requested timeframe. If set to true, invalid lookups will be skipped allowing valid cryptocurrencies to still be returned.

Default: true

### OHLCV Latest › Headers[](#ohlcv-latest/header-parameters)

`X-CMC_PRO_API_KEY`

​string · required

Your CoinMarketCap Pro API key

Default: YOUR\_API\_KEY

### OHLCV Latest › Responses[](#ohlcv-latest/responses)

200400401403429500

Successful

[Cryptocurrency\_OHLCV\_Latest\_-\_Response\_Model](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptocurrency-ohlcv-latest-response-model)

`data`

​[Cryptocurrency\_OHLCV\_Latest\_-\_Cryptocurrency\_Results\_map](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptocurrency-ohlcv-latest-cryptocurrency-results-map) · required

A map of cryptocurrency objects by ID or symbol (as passed in query parameters).

`status`

​[API\_Status\_Object](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#api-status-object)

Standardized status object for API calls.

GET/v2/cryptocurrency/ohlcv/latest

`curl --request GET \ --url https://pro-api.coinmarketcap.com/v2/cryptocurrency/ohlcv/latest \ --header 'X-CMC_PRO_API_KEY: YOUR_API_KEY'`

cURLJavaScriptPythonJavaGoC#KotlinObjective-CPHPRubySwift

Example Responses

200400401403429500

`{ "data": { "1": { "id": 1, "name": "Bitcoin", "symbol": "BTC", "last_updated": "2018-09-10T18:54:00.000Z", "time_open": "2018-09-10T00:00:00.000Z", "time_close": "2019-08-30T23:59:59.999Z", "time_high": "2018-09-10T00:00:00.000Z", "time_low": "2018-09-10T00:00:00.000Z", "quote": { "USD": { "open": 6301.57, "high": 6374.98, "low": 6292.76, "close": 6308.76, "volume": 3786450000, "last_updated": "2018-09-10T18:54:00.000Z" } } } }, "status": { "timestamp": "2026-03-05T22:43:48.471Z", "error_code": 0, "error_message": "", "elapsed": 10, "credit_count": 1, "notice": "" } }`

json

application/json

* * *

## OHLCV Historical[](#ohlcv-historical)

GET

https://pro-api.coinmarketcap.com

/v2/cryptocurrency/ohlcv/historical

Returns historical OHLCV (Open, High, Low, Close, Volume) data along with market cap for any cryptocurrency using time interval parameters. Currently daily and hourly OHLCV periods are supported. Volume is not currently supported for hourly OHLCV intervals before 2020-09-22.

**Please note**: This documentation relates to our updated V2 endpoint, which may be incompatible with our V1 versions. Documentation for deprecated endpoints can be found [the deprecated section](https://coinmarketcap.com/api/documentation/pro-api-reference/deprecated).

**Technical Notes**

-   Only the date portion of the timestamp is used for daily OHLCV so it's recommended to send an ISO date format like "2018-09-19" without time for this "time\_period".
-   One OHLCV quote will be returned for every "time\_period" between your "time\_start" (exclusive) and "time\_end" (inclusive).
-   If a "time\_start" is not supplied, the "time\_period" will be calculated in reverse from "time\_end" using the "count" parameter which defaults to 10 results.
-   If "time\_end" is not supplied, it defaults to the current time.
-   If you don't need every "time\_period" between your dates you may adjust the frequency that "time\_period" is sampled using the "interval" parameter. For example with "time\_period" set to "daily" you may set "interval" to "2d" to get the daily OHLCV for every other day. You could set "interval" to "monthly" to get the first daily OHLCV for each month, or set it to "yearly" to get the daily OHLCV value against the same date every year.

**Implementation Tips**

-   If querying for a specific OHLCV date your "time\_start" should specify a timestamp of 1 interval prior as "time\_start" is an exclusive time parameter (as opposed to "time\_end" which is inclusive to the search). This means that when you pass a "time\_start" results will be returned for the _next_ complete "time\_period". For example, if you are querying for a daily OHLCV datapoint for 2018-11-30 your "time\_start" should be "2018-11-29".
-   If only specifying a "count" parameter to return latest OHLCV periods, your "count" should be 1 number higher than the number of results you expect to receive. "Count" defines the number of "time\_period" intervals queried, _not_ the number of results to return, and this includes the currently active time period which is incomplete when working backwards from current time. For example, if you want the last daily OHLCV value available simply pass "count=2" to skip the incomplete active time period.
-   This endpoint supports requesting multiple cryptocurrencies in the same call. Please note the API response will be wrapped in an additional object in this case.

**Interval Options**

There are 2 types of time interval formats that may be used for "time\_period" and "interval" parameters. For "time\_period" these return aggregate OHLCV data from the beginning to end of each interval period. Apply these time intervals to "interval" to adjust how frequently "time\_period" is sampled.

The first are calendar year and time constants in UTC time:  
**"hourly"** - Hour intervals in UTC.  
**"daily"** - Calendar day intervals for each UTC day.  
**"weekly"** - Calendar week intervals for each calendar week.  
**"monthly"** - Calendar month intervals for each calendar month.  
**"yearly"** - Calendar year intervals for each calendar year.

The second are relative time intervals.  
**"h"**: Get the first quote available every "h" hours (3600 second intervals). Supported hour intervals are: "1h", "2h", "3h", "4h", "6h", "12h".  
**"d"**: Time periods that repeat every "d" days (86400 second intervals). Supported day intervals are: "1d", "2d", "3d", "7d", "14d", "15d", "30d", "60d", "90d", "365d".

Please note that "time\_period" currently supports the "daily" and "hourly" options. "interval" supports all interval options.

**This endpoint is available on the following [API plans](https://coinmarketcap.com/api/pricing/):**

-   ~Basic~
-   ~Builder~
-   Startup (intraday: 1 month, daily: from 2010)
-   Growth (intraday: 3 months, daily: from 2010)
-   Professional (intraday: 12 months, daily: from 2010)
-   Enterprise (intraday: from 2010, daily: from 2010)

**Note:** Intraday = `5m-12h` intervals, Daily = `1d-1y` intervals

**Cache / Update frequency:** Latest Daily OHLCV record is available ~5 to ~10 minutes after each midnight UTC. The latest hourly OHLCV record is available 5 minutes after each UTC hour.  
**Plan credit use:** 1 call credit per 100 OHLCV data points returned (rounded up) and 1 call credit per `convert` option beyond the first.  
**CMC equivalent pages:** Our historical cryptocurrency data pages like [coinmarketcap.com/currencies/bitcoin/historical-data/](https://coinmarketcap.com/currencies/bitcoin/historical-data/).

### OHLCV Historical › query Parameters[](#ohlcv-historical/query-parameters)

`id`

​string · pattern: `^\d+(?:,\d+)*$`

One or more comma-separated CoinMarketCap cryptocurrency IDs. Example: "1,1027"

`slug`

​string · pattern: `^[0-9a-z-]+(?:,[0-9a…`

Alternatively pass a comma-separated list of cryptocurrency slugs. Example: "bitcoin,ethereum"

`symbol`

​string · pattern: `^[0-9A-Za-z$@\-,]+(?…`

Alternatively pass one or more comma-separated cryptocurrency symbols. Example: "BTC,ETH". At least one "id" _or_ "slug" _or_ "symbol" is required for this request.

`time_period`

​string · enum

Time period to return OHLCV data for. The default is "daily". If hourly, the open will be 01:00 and the close will be 01:59. If daily, the open will be 00:00:00 for the day and close will be 23:59:99 for the same day. See the main endpoint description for details.

Enum values:

daily

hourly

Default: daily

`time_start`

​string

Timestamp (Unix or ISO 8601) to start returning OHLCV time periods for. Only the date portion of the timestamp is used for daily OHLCV so it's recommended to send an ISO date format like "2018-09-19" without time.

`time_end`

​string

Timestamp (Unix or ISO 8601) to stop returning OHLCV time periods for (inclusive). Optional, if not passed we'll default to the current time. Only the date portion of the timestamp is used for daily OHLCV so it's recommended to send an ISO date format like "2018-09-19" without time.

`count`

​number · min: 1 · max: 10000

Optionally limit the number of time periods to return results for. The default is 10 items. The current query limit is 10000 items.

Default: 10

`interval`

​string · enum

Optionally adjust the interval that "time\_period" is sampled. For example with interval=monthly&time\_period=daily you will see a daily OHLCV record for January, February, March and so on. See main endpoint description for available options.

Enum values:

hourly

daily

weekly

monthly

yearly

1h

2h

3h

show 13 more

Default: daily

`convert`

​string · pattern: `^[0-9A-Za-z$@\-,]+(?…`

By default market quotes are returned in USD. Optionally calculate market quotes in up to 3 fiat currencies or cryptocurrencies.

`convert_id`

​string · pattern: `^\d+(?:,\d+)*$`

Optionally calculate market quotes by CoinMarketCap ID instead of symbol. This option is identical to `convert` outside of ID format. Ex: convert\_id=1,2781 would replace convert=BTC,USD in your query. This parameter cannot be used when `convert` is used.

`skip_invalid`

​boolean

Pass `true` to relax request validation rules. When requesting records on multiple cryptocurrencies an error is returned if any invalid cryptocurrencies are requested or a cryptocurrency does not have matching records in the requested timeframe. If set to true, invalid lookups will be skipped allowing valid cryptocurrencies to still be returned.

Default: true

### OHLCV Historical › Headers[](#ohlcv-historical/header-parameters)

`X-CMC_PRO_API_KEY`

​string · required

Your CoinMarketCap Pro API key

Default: YOUR\_API\_KEY

### OHLCV Historical › Responses[](#ohlcv-historical/responses)

200400401403429500

Successful

[Cryptocurrency\_OHLCV\_Historical\_-\_Response\_Model](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptocurrency-ohlcv-historical-response-model)

`data`

​[Cryptocurrency\_OHLCV\_Historical\_-\_Results\_object](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptocurrency-ohlcv-historical-results-object) · required

Results of your query returned as an object.

`status`

​[API\_Status\_Object](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#api-status-object)

Standardized status object for API calls.

GET/v2/cryptocurrency/ohlcv/historical

`curl --request GET \ --url https://pro-api.coinmarketcap.com/v2/cryptocurrency/ohlcv/historical \ --header 'X-CMC_PRO_API_KEY: YOUR_API_KEY'`

cURLJavaScriptPythonJavaGoC#KotlinObjective-CPHPRubySwift

Example Responses

200400401403429500

`{ "data": { "id": 1, "name": "Bitcoin", "symbol": "BTC", "quotes": [ { "time_open": "2019-01-02T00:00:00.000Z", "time_close": "2019-01-02T23:59:59.999Z", "time_high": "2019-01-02T03:53:00.000Z", "time_low": "2019-01-02T02:43:00.000Z", "quote": { "USD": { "open": 3849.21640853, "high": 3947.9812729, "low": 3817.40949569, "close": 3943.40933686, "volume": 5244856835.70851, "market_cap": 68849856731.6738, "timestamp": "2019-01-02T23:59:59.999Z" } } }, { "time_open": "2019-01-03T00:00:00.000Z", "time_close": "2019-01-03T23:59:59.999Z", "time_high": "2019-01-02T03:53:00.000Z", "time_low": "2019-01-02T02:43:00.000Z", "quote": { "USD": { "open": 3931.04863841, "high": 3935.68513083, "low": 3826.22287069, "close": 3836.74131867, "volume": 4530215218.84018, "market_cap": 66994920902.7202, "timestamp": "2019-01-03T23:59:59.999Z" } } } ] }, "status": { "timestamp": "2026-03-05T22:43:48.471Z", "error_code": 0, "error_message": "", "elapsed": 10, "credit_count": 1, "notice": "" } }`

json

application/json

* * *

## Market Pairs Latest[](#market-pairs-latest)

GET

https://pro-api.coinmarketcap.com

/v2/cryptocurrency/market-pairs/latest

Lists all active market pairs that CoinMarketCap tracks for a given cryptocurrency or fiat currency. All markets with this currency as the pair base _or_ pair quote will be returned. The latest price and volume information is returned for each market. Use the "convert" option to return market values in multiple fiat and cryptocurrency conversions in the same call.

**Please note**: This documentation relates to our updated V2 endpoint, which may be incompatible with our V1 versions. Documentation for deprecated endpoints can be found [the deprecated section](https://coinmarketcap.com/api/documentation/pro-api-reference/deprecated).

**This endpoint is available on the following [API plans](https://coinmarketcap.com/api/pricing/):**

-   ~Basic~
-   ~Builder~
-   ~Startup~
-   Growth
-   Professional
-   Enterprise

**Cache / Update frequency:** Every 1 minute.  
**Plan credit use:** 1 call credit per 100 market pairs returned (rounded up) and 1 call credit per `convert` option beyond the first.  
**CMC equivalent pages:** Our active cryptocurrency markets pages like [coinmarketcap.com/currencies/bitcoin/#markets](https://coinmarketcap.com/currencies/bitcoin/#markets).

### Market Pairs Latest › query Parameters[](#market-pairs-latest/query-parameters)

`id`

​string

A cryptocurrency or fiat currency by CoinMarketCap ID to list market pairs for. Example: "1"

`slug`

​string · pattern: `^[0-9a-z-]*$`

Alternatively pass a cryptocurrency by slug. Example: "bitcoin"

`symbol`

​string · pattern: `^[0-9A-Za-z$@\-]*$`

Alternatively pass a cryptocurrency by symbol. Fiat currencies are not supported by this field. Example: "BTC". A single cryptocurrency "id", "slug", _or_ "symbol" is required.

`start`

​integer · min: 1

Optionally offset the start (1-based index) of the paginated list of items to return.

Default: 1

`limit`

​integer · min: 1 · max: 5000

Optionally specify the number of results to return. Use this parameter and the "start" parameter to determine your own pagination size.

Default: 100

`sort_dir`

​string · enum

Optionally specify the sort direction of markets returned.

Enum values:

asc

desc

Default: desc

`sort`

​string · enum

Optionally specify the sort order of markets returned. By default we return a strict sort on 24 hour reported volume. Pass `cmc_rank` to return a CMC methodology based sort where markets with excluded volumes are returned last.

Enum values:

volume\_24h\_strict

cmc\_rank

cmc\_rank\_advanced

effective\_liquidity

market\_score

market\_reputation

Default: volume\_24h\_strict

`aux`

​string · pattern: `^(num_market_pairs|c…`

Optionally specify a comma-separated list of supplemental data fields to return. Pass `num_market_pairs,category,fee_type,market_url,currency_name,currency_slug,price_quote,notice,cmc_rank,effective_liquidity,market_score,market_reputation` to include all auxiliary fields.

Default: num\_market\_pairs,category,fee\_type

`matched_id`

​string · pattern: `^\d+(?:,\d+)*$`

Optionally include one or more fiat or cryptocurrency IDs to filter market pairs by. For example `?id=1&matched_id=2781` would only return BTC markets that matched: "BTC/USD" or "USD/BTC". This parameter cannot be used when `matched_symbol` is used.

`matched_symbol`

​string · pattern: `^[0-9A-Za-z$@\-,]+(?…`

Optionally include one or more fiat or cryptocurrency symbols to filter market pairs by. For example `?symbol=BTC&matched_symbol=USD` would only return BTC markets that matched: "BTC/USD" or "USD/BTC". This parameter cannot be used when `matched_id` is used.

`category`

​string · enum

The category of trading this market falls under. Spot markets are the most common but options include derivatives and OTC.

Enum values:

all

spot

derivatives

otc

perpetual

Default: all

`fee_type`

​string · enum

The fee type the exchange enforces for this market.

Enum values:

all

percentage

no-fees

transactional-mining

unknown

Default: all

`convert`

​string · pattern: `^[0-9A-Za-z$@\-,]+(?…`

Optionally calculate market quotes in up to 120 currencies at once by passing a comma-separated list of cryptocurrency or fiat currency symbols. Each additional convert option beyond the first requires an additional call credit. A list of supported fiat options can be found [here](https://coinmarketcap.com/api/documentation/guides/standards-and-conventions). Each conversion is returned in its own "quote" object.

`convert_id`

​string · pattern: `^\d+(?:,\d+)*$`

Optionally calculate market quotes by CoinMarketCap ID instead of symbol. This option is identical to `convert` outside of ID format. Ex: convert\_id=1,2781 would replace convert=BTC,USD in your query. This parameter cannot be used when `convert` is used.

### Market Pairs Latest › Headers[](#market-pairs-latest/header-parameters)

`X-CMC_PRO_API_KEY`

​string · required

Your CoinMarketCap Pro API key

Default: YOUR\_API\_KEY

### Market Pairs Latest › Responses[](#market-pairs-latest/responses)

200400401403429500

Successful

[Cryptocurrency\_Market\_Pairs\_Latest\_-\_Response\_Model](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptocurrency-market-pairs-latest-response-model)

`data`

​[Cryptocurrency\_Market\_Pairs\_Latest\_-\_Results\_object](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptocurrency-market-pairs-latest-results-object) · required

Results of your query returned as an object.

`status`

​[API\_Status\_Object](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#api-status-object)

Standardized status object for API calls.

GET/v2/cryptocurrency/market-pairs/latest

`curl --request GET \ --url https://pro-api.coinmarketcap.com/v2/cryptocurrency/market-pairs/latest \ --header 'X-CMC_PRO_API_KEY: YOUR_API_KEY'`

cURLJavaScriptPythonJavaGoC#KotlinObjective-CPHPRubySwift

Example Responses

200400401403429500

`{ "data": { "id": 1, "name": "Bitcoin", "symbol": "BTC", "num_market_pairs": 7526, "market_pairs": [ { "exchange": { "id": 157, "name": "BitMEX", "slug": "bitmex" }, "market_id": 4902, "market_pair": "BTC/USD", "category": "derivatives", "fee_type": "no-fees", "market_pair_base": { "currency_id": 1, "currency_symbol": "BTC", "exchange_symbol": "XBT", "currency_type": "cryptocurrency" }, "market_pair_quote": { "currency_id": 2781, "currency_symbol": "USD", "exchange_symbol": "USD", "currency_type": "fiat" }, "quote": { "exchange_reported": { "price": 7839, "volume_24h_base": 434215.85308502, "volume_24h_quote": 3403818072.33347, "last_updated": "2019-05-24T02:39:00.000Z" }, "USD": { "price": 7839, "volume_24h": 3403818072.33347, "last_updated": "2019-05-24T02:39:00.000Z" } } }, { "exchange": { "id": 108, "name": "Negocie Coins", "slug": "negocie-coins" }, "market_id": 3377, "market_pair": "BTC/BRL", "category": "spot", "fee_type": "percentage", "market_pair_base": { "currency_id": 1, "currency_symbol": "BTC", "exchange_symbol": "BTC", "currency_type": "cryptocurrency" }, "market_pair_quote": { "currency_id": 2783, "currency_symbol": "BRL", "exchange_symbol": "BRL", "currency_type": "fiat" }, "quote": { "exchange_reported": { "price": 33002.11, "volume_24h_base": 336699.03559957, "volume_24h_quote": 11111778609.7509, "last_updated": "2019-05-24T02:39:00.000Z" }, "USD": { "price": 8165.02539531659, "volume_24h": 2749156176.2491, "last_updated": "2019-05-24T02:39:00.000Z" } } } ] }, "status": { "timestamp": "2026-03-05T22:43:48.471Z", "error_code": 0, "error_message": "", "elapsed": 10, "credit_count": 1, "notice": "" } }`

json

application/json

* * *

## Price Performance Stats[](#price-performance-stats)

GET

https://pro-api.coinmarketcap.com

/v2/cryptocurrency/price-performance-stats/latest

Returns price performance statistics for one or more cryptocurrencies including launch price ROI and all-time high / all-time low. Stats are returned for an `all_time` period by default. UTC `yesterday` and a number of _rolling time periods_ may be requested using the `time_period` parameter. Utilize the `convert` parameter to translate values into multiple fiats or cryptocurrencies using historical rates.

**Please note**: This documentation relates to our updated V2 endpoint, which may be incompatible with our V1 versions. Documentation for deprecated endpoints can be found [the deprecated section](https://coinmarketcap.com/api/documentation/pro-api-reference/deprecated).

**This endpoint is available on the following [API plans](https://coinmarketcap.com/api/pricing/):**

-   ~Basic~
-   ~Builder~
-   Startup
-   Growth
-   Professional
-   Enterprise

**Cache / Update frequency:** Every 60 seconds.  
**Plan credit use:** 1 call credit per 100 cryptocurrencies returned (rounded up) and 1 call credit per `convert` option beyond the first.  
**CMC equivalent pages:** The statistics module displayed on cryptocurrency pages like [Bitcoin](https://coinmarketcap.com/currencies/bitcoin/).

_**NOTE:** You may also use `/cryptocurrency/ohlcv/historical` for traditional OHLCV data at historical daily and hourly intervals. You may also use `/v1/cryptocurrency/ohlcv/latest` for OHLCV data for the current UTC day._

### Price Performance Stats › query Parameters[](#price-performance-stats/query-parameters)

`id`

​string · pattern: `^\d+(?:,\d+)*$`

One or more comma-separated cryptocurrency CoinMarketCap IDs. Example: 1,2

`slug`

​string · pattern: `^[0-9a-z-]+(?:,[0-9a…`

Alternatively pass a comma-separated list of cryptocurrency slugs. Example: "bitcoin,ethereum"

`symbol`

​string · pattern: `^[0-9A-Za-z$@\-,]+(?…`

Alternatively pass one or more comma-separated cryptocurrency symbols. Example: "BTC,ETH". At least one "id" _or_ "slug" _or_ "symbol" is required for this request.

`time_period`

​string · pattern: `^(all_time|yesterday…`

Specify one or more comma-delimited time periods to return stats for. `all_time` is the default. Pass `all_time,yesterday,24h,7d,30d,90d,365d` to return all supported time periods. All rolling periods have a rolling close time of the current request time. For example `24h` would have a close time of now and an open time of 24 hours before now. _Please note: `yesterday` is a UTC period and currently does not currently support `high` and `low` timestamps._

Default: all\_time

`convert`

​string · pattern: `^[0-9A-Za-z$@\-,]+(?…`

Optionally calculate quotes in up to 120 currencies at once by passing a comma-separated list of cryptocurrency or fiat currency symbols. Each additional convert option beyond the first requires an additional call credit. A list of supported fiat options can be found [here](https://coinmarketcap.com/api/documentation/guides/standards-and-conventions). Each conversion is returned in its own "quote" object.

`convert_id`

​string · pattern: `^\d+(?:,\d+)*$`

Optionally calculate quotes by CoinMarketCap ID instead of symbol. This option is identical to `convert` outside of ID format. Ex: convert\_id=1,2781 would replace convert=BTC,USD in your query. This parameter cannot be used when `convert` is used.

`skip_invalid`

​boolean

Pass `true` to relax request validation rules. When requesting records on multiple cryptocurrencies an error is returned if no match is found for 1 or more requested cryptocurrencies. If set to true, invalid lookups will be skipped allowing valid cryptocurrencies to still be returned.

Default: true

### Price Performance Stats › Headers[](#price-performance-stats/header-parameters)

`X-CMC_PRO_API_KEY`

​string · required

Your CoinMarketCap Pro API key

Default: YOUR\_API\_KEY

### Price Performance Stats › Responses[](#price-performance-stats/responses)

200400401403429500

Successful

[Cryptocurrency\_Price\_Performance\_Stats\_Latest\_-\_Response\_Model](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptocurrency-price-performance-stats-latest-response-model)

`data`

​[Cryptocurrency\_Price\_Performance\_Stats\_Latest\_-\_Cryptocurrency\_Results\_map](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptocurrency-price-performance-stats-latest-cryptocurrency-results-map) · required

An object map of cryptocurrency objects by ID, slug, or symbol (as used in query parameters).

`status`

​[API\_Status\_Object](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#api-status-object)

Standardized status object for API calls.

GET/v2/cryptocurrency/price-performance-stats/latest

`curl --request GET \ --url https://pro-api.coinmarketcap.com/v2/cryptocurrency/price-performance-stats/latest \ --header 'X-CMC_PRO_API_KEY: YOUR_API_KEY'`

cURLJavaScriptPythonJavaGoC#KotlinObjective-CPHPRubySwift

Example Responses

200400401403429500

`{ "data": { "1": { "id": 1, "name": "Bitcoin", "symbol": "BTC", "slug": "bitcoin", "last_updated": "2019-08-22T01:51:32.000Z", "periods": { "USD": { "open_timestamp": "2013-04-28T00:00:00.000Z", "high_timestamp": "2017-12-17T12:19:14.000Z", "low_timestamp": "2013-07-05T18:56:01.000Z", "close_timestamp": "2019-08-22T01:52:18.613Z", "quote": { "USD": { "open": 135.3000030517578, "open_timestamp": "2013-04-28T00:00:00.000Z", "high": 20088.99609375, "high_timestamp": "2017-12-17T12:19:14.000Z", "low": 65.5260009765625, "low_timestamp": "2013-07-05T18:56:01.000Z", "close": 65.5260009765625, "close_timestamp": "2019-08-22T01:52:18.618Z", "percent_change": 7223.718930042746, "price_change": 9773.691932798241 } } } } } }, "status": { "timestamp": "2026-03-05T22:43:48.471Z", "error_code": 0, "error_message": "", "elapsed": 10, "credit_count": 1, "notice": "" } }`

json

application/json

* * *

## Metadata[](#metadata)

GET

https://pro-api.coinmarketcap.com

/v2/cryptocurrency/info

Returns all static metadata available for one or more cryptocurrencies. This information includes details like logo, description, official website URL, social links, and links to a cryptocurrency's technical documentation.

**Please note**: This documentation relates to our updated V2 endpoint, which may be incompatible with our V1 versions. Documentation for deprecated endpoints can be found [the deprecated section](https://coinmarketcap.com/api/documentation/pro-api-reference/deprecated).

**This endpoint is available on the following [API plans](https://coinmarketcap.com/api/pricing/):**

-   Basic
-   Startup
-   Builder
-   Growth
-   Professional
-   Enterprise

Available with no API key

Call this endpoint keyless - no API key, no signup. Prefix the path with `/public-api`: `https://pro-api.coinmarketcap.com/public-api/v2/cryptocurrency/info`. See the [Keyless Public API](https://coinmarketcap.com/api/documentation/pro-api-reference/keyless-public-api) for the full list, rate limits, and examples.

**Cache / Update frequency:** Static data is updated only as needed, every 30 seconds.  
**Plan credit use:** 1 call credit per 100 cryptocurrencies returned (rounded up).  
**CMC equivalent pages:** Cryptocurrency detail page metadata like [coinmarketcap.com/currencies/bitcoin/](https://coinmarketcap.com/currencies/bitcoin/).

### Metadata › query Parameters[](#metadata/query-parameters)

`id`

​string · pattern: `^\d+(?:,\d+)*$`

One or more comma-separated CoinMarketCap cryptocurrency IDs. Example: "1,2"

`slug`

​string · pattern: `^[0-9a-z-]+(?:,[0-9a…`

Alternatively pass a comma-separated list of cryptocurrency slugs. Example: "bitcoin,ethereum"

`symbol`

​string · pattern: `^[0-9A-Za-z$@\-,]+(?…`

Alternatively pass one or more comma-separated cryptocurrency symbols. Example: "BTC,ETH". At least one "id" _or_ "slug" _or_ "symbol" is required for this request. Please note that starting in the v2 endpoint, due to the fact that a symbol is not unique, if you request by symbol each data response will contain an array of objects containing all of the coins that use each requested symbol. The v1 endpoint will still return a single object, the highest ranked coin using that symbol.

`address`

​string

Alternatively pass in a contract address. Example: "0xc40af1e4fecfa05ce6bab79dcd8b373d2e436c4e"

`skip_invalid`

​boolean

Pass `true` to relax request validation rules. When requesting records on multiple cryptocurrencies an error is returned if any invalid cryptocurrencies are requested or a cryptocurrency does not have matching records in the requested timeframe. If set to true, invalid lookups will be skipped allowing valid cryptocurrencies to still be returned.

Default: false

`aux`

​string · pattern: `^(urls|logo|descript…`

Optionally specify a comma-separated list of supplemental data fields to return. Pass `urls,logo,description,tags,platform,date_added,notice,status` to include all auxiliary fields.

Default: urls,logo,description,tags,platform,date\_added,notice

### Metadata › Headers[](#metadata/header-parameters)

`X-CMC_PRO_API_KEY`

​string · required

Your CoinMarketCap Pro API key

Default: YOUR\_API\_KEY

### Metadata › Responses[](#metadata/responses)

200400401403429500

Successful

[Cryptocurrencies\_Info\_-\_Response\_Model](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptocurrencies-info-response-model)

`data`

​[Cryptocurrency\_Info\_-\_Results\_map](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptocurrency-info-results-map) · required

Results of your query returned as an object map.

`status`

​[API\_Status\_Object](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#api-status-object)

Standardized status object for API calls.

GET/v2/cryptocurrency/info

`curl --request GET \ --url https://pro-api.coinmarketcap.com/v2/cryptocurrency/info \ --header 'X-CMC_PRO_API_KEY: YOUR_API_KEY'`

cURLJavaScriptPythonJavaGoC#KotlinObjective-CPHPRubySwift

Example Responses

200400401403429500

`{ "data": { "1": { "urls": { "website": [ "https://bitcoin.org/" ], "technical_doc": [ "https://bitcoin.org/bitcoin.pdf" ], "twitter": [], "reddit": [ "https://reddit.com/r/bitcoin" ], "message_board": [ "https://bitcointalk.org" ], "announcement": [], "chat": [], "explorer": [ "https://blockchain.coinmarketcap.com/chain/bitcoin", "https://blockchain.info/", "https://live.blockcypher.com/btc/" ], "source_code": [ "https://github.com/bitcoin/" ] }, "logo": "https://s2.coinmarketcap.com/static/img/coins/64x64/1.png", "id": 1, "name": "Bitcoin", "symbol": "BTC", "slug": "bitcoin", "description": "Bitcoin (BTC) is a consensus network that enables a new payment system and a completely digital currency. Powered by its users, it is a peer to peer payment network that requires no central authority to operate. On October 31st, 2008, an individual or group of individuals operating under the pseudonym \"Satoshi Nakamoto\" published the Bitcoin Whitepaper and described it as: \"a purely peer-to-peer version of electronic cash would allow online payments to be sent directly from one party to another without going through a financial institution.\"", "date_added": "2013-04-28T00:00:00.000Z", "date_launched": "2013-04-28T00:00:00.000Z", "tags": [ "mineable" ], "platform": null, "category": "coin" } }, "status": { "timestamp": "2026-03-05T22:43:48.471Z", "error_code": 0, "error_message": "", "elapsed": 10, "credit_count": 1, "notice": "" } }`

json

application/json

* * *

## Trending Latest[](#trending-latest)

GET

https://pro-api.coinmarketcap.com

/v1/cryptocurrency/trending/latest

Returns a paginated list of all trending cryptocurrency market data, determined and sorted by CoinMarketCap search volume.

**This endpoint is available on the following [API plans](https://coinmarketcap.com/api/pricing/):**

-   Startup
-   Growth
-   Professional
-   Enterprise

**Cache / Update frequency:** Every 10 minutes.  
**Plan credit use:** 1 call credit per 200 cryptocurrencies returned (rounded up) and 1 call credit per `convert` option beyond the first.  
**CMC equivalent pages:** Our cryptocurrency Trending page [coinmarketcap.com/trending-cryptocurrencies/](https://coinmarketcap.com/trending-cryptocurrencies/).

### Trending Latest › query Parameters[](#trending-latest/query-parameters)

`start`

​integer · min: 1

Optionally offset the start (1-based index) of the paginated list of items to return.

Default: 1

`limit`

​integer · min: 1 · max: 1000

Optionally specify the number of results to return. Use this parameter and the "start" parameter to determine your own pagination size.

Default: 100

`time_period`

​string · enum

Adjusts the overall window of time for the latest trending coins.

Enum values:

24h

30d

7d

Default: 24h

`convert`

​string · pattern: `^[0-9A-Za-z$@\-,]+(?…`

Optionally calculate market quotes in up to 120 currencies at once by passing a comma-separated list of cryptocurrency or fiat currency symbols. Each additional convert option beyond the first requires an additional call credit. A list of supported fiat options can be found [here](https://coinmarketcap.com/api/documentation/guides/standards-and-conventions). Each conversion is returned in its own "quote" object.

`convert_id`

​string · pattern: `^\d+(?:,\d+)*$`

Optionally calculate market quotes by CoinMarketCap ID instead of symbol. This option is identical to `convert` outside of ID format. Ex: convert\_id=1,2781 would replace convert=BTC,USD in your query. This parameter cannot be used when `convert` is used.

### Trending Latest › Headers[](#trending-latest/header-parameters)

`X-CMC_PRO_API_KEY`

​string · required

Your CoinMarketCap Pro API key

Default: YOUR\_API\_KEY

### Trending Latest › Responses[](#trending-latest/responses)

200400401403429500

Successful

[Cryptocurrency\_Trending\_Latest\_-\_Response\_Model](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptocurrency-trending-latest-response-model)

`data`

​[Cryptocurrency\_Trending\_Latest\_-\_Cryptocurrency\_object\[\]](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptocurrency-trending-latest-cryptocurrency-object) · required

Array of cryptocurrency objects matching the list options.

`status`

​[API\_Status\_Object](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#api-status-object)

Standardized status object for API calls.

GET/v1/cryptocurrency/trending/latest

`curl --request GET \ --url https://pro-api.coinmarketcap.com/v1/cryptocurrency/trending/latest \ --header 'X-CMC_PRO_API_KEY: YOUR_API_KEY'`

cURLJavaScriptPythonJavaGoC#KotlinObjective-CPHPRubySwift

Example Responses

200400401403429500

`{ "data": [ { "id": 1, "name": "Bitcoin", "symbol": "BTC", "slug": "bitcoin", "cmc_rank": 5, "is_active": 1, "is_fiat": 0, "self_reported_circulating_supply": null, "self_reported_market_cap": null, "num_market_pairs": 500, "circulating_supply": 16950100, "total_supply": 16950100, "max_supply": 21000000, "last_updated": "2018-06-02T22:51:28.209Z", "date_added": "2013-04-28T00:00:00.000Z", "tags": [ "mineable" ], "platform": null, "quote": { "USD": { "price": 9283.92, "volume_24h": 7155680000, "percent_change_1h": -0.152774, "percent_change_24h": 0.518894, "percent_change_7d": 0.986573, "market_cap": 158055024432, "last_updated": "2018-08-09T22:53:32.000Z" } } }, { "id": 1027, "name": "Ethereum", "symbol": "ETH", "slug": "ethereum", "num_market_pairs": 6360, "circulating_supply": 16950100, "total_supply": 16950100, "max_supply": 21000000, "last_updated": "2018-06-02T22:51:28.209Z", "date_added": "2013-04-28T00:00:00.000Z", "tags": [ "mineable" ], "platform": null, "quote": { "USD": { "price": 1283.92, "volume_24h": 7155680000, "percent_change_1h": -0.152774, "percent_change_24h": 0.518894, "percent_change_7d": 0.986573, "market_cap": 158055024432, "last_updated": "2018-08-09T22:53:32.000Z" } } } ], "status": { "timestamp": "2018-06-02T22:51:28.209Z", "error_code": 0, "error_message": "", "elapsed": 10, "credit_count": 1 } }`

json

application/json

* * *

## Trending Gainers & Losers[](#trending-gainers-losers)

GET

https://pro-api.coinmarketcap.com

/v1/cryptocurrency/trending/gainers-losers

Returns a paginated list of all trending cryptocurrencies, determined and sorted by the largest price gains or losses.

You may sort against any of the following:  
**percent\_change\_24h**: 24 hour trading price percentage change for each currency.

**This endpoint is available on the following [API plans](https://coinmarketcap.com/api/pricing/):**

-   Startup
-   Growth
-   Professional
-   Enterprise

**Cache / Update frequency:** Every 10 minutes.  
**Plan credit use:** 1 call credit per 200 cryptocurrencies returned (rounded up) and 1 call credit per `convert` option beyond the first.  
**CMC equivalent pages:** Our cryptocurrency Gainers & Losers page [coinmarketcap.com/gainers-losers/](https://coinmarketcap.com/gainers-losers/).

### Trending Gainers & Losers › query Parameters[](#trending-gainers-losers/query-parameters)

`start`

​integer · min: 1

Optionally offset the start (1-based index) of the paginated list of items to return.

Default: 1

`limit`

​integer · min: 1 · max: 1000

Optionally specify the number of results to return. Use this parameter and the "start" parameter to determine your own pagination size.

Default: 100

`time_period`

​string · enum

Adjusts the overall window of time for the biggest gainers and losers.

Enum values:

1h

24h

30d

7d

Default: 24h

`convert`

​string · pattern: `^[0-9A-Za-z$@\-,]+(?…`

Optionally calculate market quotes in up to 120 currencies at once by passing a comma-separated list of cryptocurrency or fiat currency symbols. Each additional convert option beyond the first requires an additional call credit. A list of supported fiat options can be found [here](https://coinmarketcap.com/api/documentation/guides/standards-and-conventions). Each conversion is returned in its own "quote" object.

`convert_id`

​string · pattern: `^\d+(?:,\d+)*$`

Optionally calculate market quotes by CoinMarketCap ID instead of symbol. This option is identical to `convert` outside of ID format. Ex: convert\_id=1,2781 would replace convert=BTC,USD in your query. This parameter cannot be used when `convert` is used.

`sort`

​string · enum

What field to sort the list of cryptocurrencies by.

Enum values:

percent\_change\_24h

Default: percent\_change\_24h

`sort_dir`

​string · enum

The direction in which to order cryptocurrencies against the specified sort.

Enum values:

asc

desc

### Trending Gainers & Losers › Headers[](#trending-gainers-losers/header-parameters)

`X-CMC_PRO_API_KEY`

​string · required

Your CoinMarketCap Pro API key

Default: YOUR\_API\_KEY

### Trending Gainers & Losers › Responses[](#trending-gainers-losers/responses)

200400401403429500

Successful

[Cryptocurrency\_Trending\_Gainers\_Losers\_-\_Response\_Model](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptocurrency-trending-gainers-losers-response-model)

`data`

​[Cryptocurrency\_-\_Cryptocurrency\_object\[\]](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptocurrency-cryptocurrency-object) · required

Array of cryptocurrency objects matching the list options.

`status`

​[API\_Status\_Object](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#api-status-object)

Standardized status object for API calls.

GET/v1/cryptocurrency/trending/gainers-losers

`curl --request GET \ --url https://pro-api.coinmarketcap.com/v1/cryptocurrency/trending/gainers-losers \ --header 'X-CMC_PRO_API_KEY: YOUR_API_KEY'`

cURLJavaScriptPythonJavaGoC#KotlinObjective-CPHPRubySwift

Example Responses

200400401403429500

`{ "data": [ { "id": 1, "name": "Bitcoin", "symbol": "BTC", "slug": "bitcoin", "cmc_rank": 5, "num_market_pairs": 500, "circulating_supply": 16950100, "total_supply": 16950100, "max_supply": 21000000, "last_updated": "2018-06-02T22:51:28.209Z", "date_added": "2013-04-28T00:00:00.000Z", "tags": [ "mineable" ], "platform": null, "quote": { "USD": { "price": 9283.92, "volume_24h": 7155680000, "percent_change_1h": -0.152774, "percent_change_24h": 0.518894, "percent_change_7d": 0.986573, "market_cap": 158055024432, "last_updated": "2018-08-09T22:53:32.000Z" } } }, { "id": 1027, "name": "Ethereum", "symbol": "ETH", "slug": "ethereum", "num_market_pairs": 6360, "circulating_supply": 16950100, "total_supply": 16950100, "max_supply": 21000000, "last_updated": "2018-06-02T22:51:28.209Z", "date_added": "2013-04-28T00:00:00.000Z", "tags": [ "mineable" ], "platform": null, "quote": { "USD": { "price": 1283.92, "volume_24h": 7155680000, "percent_change_1h": -0.152774, "percent_change_24h": 0.518894, "percent_change_7d": 0.986573, "market_cap": 158055024432, "last_updated": "2018-08-09T22:53:32.000Z" } } } ], "status": { "timestamp": "2018-06-02T22:51:28.209Z", "error_code": 0, "error_message": "", "elapsed": 10, "credit_count": 1 } }`

json

application/json

* * *

## Trending Most Visited[](#trending-most-visited)

GET

https://pro-api.coinmarketcap.com

/v1/cryptocurrency/trending/most-visited

Returns a paginated list of all trending cryptocurrency market data, determined and sorted by traffic to coin detail pages.

**This endpoint is available on the following [API plans](https://coinmarketcap.com/api/pricing/):**

-   Startup
-   Growth
-   Professional
-   Enterprise

**Cache / Update frequency:** Every 24 hours.  
**Plan credit use:** 1 call credit per 200 cryptocurrencies returned (rounded up) and 1 call credit per `convert` option beyond the first.  
**CMC equivalent pages:** The CoinMarketCap “Most Visited” trending list. [coinmarketcap.com/most-viewed-pages/](https://coinmarketcap.com/most-viewed-pages/).

### Trending Most Visited › query Parameters[](#trending-most-visited/query-parameters)

`start`

​integer · min: 1

Optionally offset the start (1-based index) of the paginated list of items to return.

Default: 1

`limit`

​integer · min: 1 · max: 1000

Optionally specify the number of results to return. Use this parameter and the "start" parameter to determine your own pagination size.

Default: 100

`time_period`

​string · enum

Adjusts the overall window of time for most visited currencies.

Enum values:

24h

30d

7d

Default: 24h

`convert`

​string · pattern: `^[0-9A-Za-z$@\-,]+(?…`

Optionally calculate market quotes in up to 120 currencies at once by passing a comma-separated list of cryptocurrency or fiat currency symbols. Each additional convert option beyond the first requires an additional call credit. A list of supported fiat options can be found [here](https://coinmarketcap.com/api/documentation/guides/standards-and-conventions). Each conversion is returned in its own "quote" object.

`convert_id`

​string · pattern: `^\d+(?:,\d+)*$`

Optionally calculate market quotes by CoinMarketCap ID instead of symbol. This option is identical to `convert` outside of ID format. Ex: convert\_id=1,2781 would replace convert=BTC,USD in your query. This parameter cannot be used when `convert` is used.

### Trending Most Visited › Headers[](#trending-most-visited/header-parameters)

`X-CMC_PRO_API_KEY`

​string · required

Your CoinMarketCap Pro API key

Default: YOUR\_API\_KEY

### Trending Most Visited › Responses[](#trending-most-visited/responses)

200400401403429500

Successful

[Cryptocurrency\_Trending\_Most\_Visited\_-\_Response\_Model](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptocurrency-trending-most-visited-response-model)

`data`

​[Cryptocurrency\_-\_Cryptocurrency\_object\[\]](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#cryptocurrency-cryptocurrency-object) · required

Array of cryptocurrency objects matching the list options.

`status`

​[API\_Status\_Object](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#api-status-object)

Standardized status object for API calls.

GET/v1/cryptocurrency/trending/most-visited

`curl --request GET \ --url https://pro-api.coinmarketcap.com/v1/cryptocurrency/trending/most-visited \ --header 'X-CMC_PRO_API_KEY: YOUR_API_KEY'`

cURLJavaScriptPythonJavaGoC#KotlinObjective-CPHPRubySwift

Example Responses

200400401403429500

`{ "data": [ { "id": 1, "name": "Bitcoin", "symbol": "BTC", "slug": "bitcoin", "cmc_rank": 5, "num_market_pairs": 500, "circulating_supply": 16950100, "total_supply": 16950100, "max_supply": 21000000, "last_updated": "2018-06-02T22:51:28.209Z", "date_added": "2013-04-28T00:00:00.000Z", "tags": [ "mineable" ], "platform": null, "quote": { "USD": { "price": 9283.92, "volume_24h": 7155680000, "percent_change_1h": -0.152774, "percent_change_24h": 0.518894, "percent_change_7d": 0.986573, "market_cap": 158055024432, "last_updated": "2018-08-09T22:53:32.000Z" } } }, { "id": 1027, "name": "Ethereum", "symbol": "ETH", "slug": "ethereum", "num_market_pairs": 6360, "circulating_supply": 16950100, "total_supply": 16950100, "max_supply": 21000000, "last_updated": "2018-06-02T22:51:28.209Z", "date_added": "2013-04-28T00:00:00.000Z", "tags": [ "mineable" ], "platform": null, "quote": { "USD": { "price": 1283.92, "volume_24h": 7155680000, "percent_change_1h": -0.152774, "percent_change_24h": 0.518894, "percent_change_7d": 0.986573, "market_cap": 158055024432, "last_updated": "2018-08-09T22:53:32.000Z" } } } ], "status": { "timestamp": "2018-06-02T22:51:28.209Z", "error_code": 0, "error_message": "", "elapsed": 10, "credit_count": 1 } }`

json

application/json

* * *

## Simple Price[](#simple-price)

GET

https://pro-api.coinmarketcap.com

/v1/simple/price

Returns current USD spot prices for one or more coins. Optimized for low latency and minimal payload size. Optional flags include market cap, 24h volume, 24h percent change, and last updated timestamp.

**This endpoint is available on the following [API plans](https://coinmarketcap.com/api/pricing/):**

-   Basic
-   Builder
-   Startup
-   Growth
-   Professional
-   Enterprise

Available with no API key

Call this endpoint keyless - no API key, no signup. Prefix the path with `/public-api`: `https://pro-api.coinmarketcap.com/public-api/v1/simple/price`. See the [Keyless Public API](https://coinmarketcap.com/api/documentation/pro-api-reference/keyless-public-api) for the full list, rate limits, and examples.

**Cache / Update frequency:** Every 60 seconds. **Plan credit use:** 1 API call credit per request.

### Simple Price › query Parameters[](#simple-price/query-parameters)

`ids`

​string · pattern: `^\d+(?:,\d+){0,49}$` · required

Comma-separated list of CoinMarketCap cryptocurrency IDs. Example: "1,1027". Max query size 50.

`include_market_cap`

​boolean

Include market cap values in the response.

Default: false

`include_volume_24h`

​boolean

Include 24-hour volume in the response.

Default: false

`include_percent_change_24h`

​boolean

Include 24-hour price change percentage in the response.

Default: false

`include_last_updated`

​boolean

Include last updated timestamp in the response.

Default: false

### Simple Price › Headers[](#simple-price/header-parameters)

`X-CMC_PRO_API_KEY`

​string · required

Your CoinMarketCap Pro API key

Default: YOUR\_API\_KEY

### Simple Price › Responses[](#simple-price/responses)

200400401403429500

Successful

[Simple\_Price\_-\_Response\_Model](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#simple-price-response-model)

`data`

​[Simple\_Price\_-\_Item\_Object\[\]](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#simple-price-item-object) · required

`status`

​[API\_Status\_Object](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#api-status-object)

Standardized status object for API calls.

GET/v1/simple/price

`curl --request GET \ --url 'https://pro-api.coinmarketcap.com/v1/simple/price?ids=%3Cstring%3E' \ --header 'X-CMC_PRO_API_KEY: YOUR_API_KEY'`

cURLJavaScriptPythonJavaGoC#KotlinObjective-CPHPRubySwift

Example Responses

200400401403429500

`{ "data": [ { "id": 1, "price": 65432.12, "market_cap": 1284000000000, "volume_24h": 32500000000, "percent_change_24h": 1.23, "last_updated": "2024-09-30T12:00:00.000Z" } ], "status": { "timestamp": "2026-03-05T22:43:48.471Z", "error_code": 0, "error_message": "", "elapsed": 10, "credit_count": 1, "notice": "" } }`

json

application/json

* * *

## Categories[](#categories)

GET

https://pro-api.coinmarketcap.com

/v1/cryptocurrency/categories

Returns information about all coin categories available on CoinMarketCap. Includes a paginated list of cryptocurrency quotes and metadata from each category.

**This endpoint is available on the following [API plans](https://coinmarketcap.com/api/pricing/):**

-   Free
-   Builder
-   Startup
-   Growth
-   Professional
-   Enterprise

Available with no API key

Call this endpoint keyless - no API key, no signup. Prefix the path with `/public-api`: `https://pro-api.coinmarketcap.com/public-api/v1/cryptocurrency/categories`. See the [Keyless Public API](https://coinmarketcap.com/api/documentation/pro-api-reference/keyless-public-api) for the full list, rate limits, and examples.

**Cache / Update frequency:** Data is updated only as needed, every 30 seconds.  
**Plan credit use:** 1 API call credit per request + 1 call credit per 200 cryptocurrencies returned (rounded up) and 1 call credit per `convert` option beyond the first.  
**CMC equivalent pages:** Our free airdrops page [coinmarketcap.com/cryptocurrency-category/](https://coinmarketcap.com/cryptocurrency-category/).

### Categories › query Parameters[](#categories/query-parameters)

`start`

​integer · min: 1

Optionally offset the start (1-based index) of the paginated list of items to return.

Default: 1

`limit`

​integer · min: 1 · max: 5000

Optionally specify the number of results to return. Use this parameter and the "start" parameter to determine your own pagination size.

`id`

​string · pattern: `^\d+(?:,\d+)*$`

Filtered categories by one or more comma-separated cryptocurrency CoinMarketCap IDs. Example: 1,2

`slug`

​string · pattern: `^[0-9a-z-]+(?:,[0-9a…`

Alternatively filter categories by a comma-separated list of cryptocurrency slugs. Example: "bitcoin,ethereum"

`symbol`

​string · pattern: `^[0-9A-Za-z$@\-,]+(?…`

Alternatively filter categories one or more comma-separated cryptocurrency symbols. Example: "BTC,ETH".

### Categories › Headers[](#categories/header-parameters)

`X-CMC_PRO_API_KEY`

​string · required

Your CoinMarketCap Pro API key

Default: YOUR\_API\_KEY

### Categories › Responses[](#categories/responses)

200400401403429500

Successful

[Categories\_-\_Response\_Model](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#categories-response-model)

`data`

​[Categories\_-\_Category\_object\[\]](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#categories-category-object) · required

Results of your query returned as an object map.

`status`

​[API\_Status\_Object](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#api-status-object)

Standardized status object for API calls.

GET/v1/cryptocurrency/categories

`curl --request GET \ --url https://pro-api.coinmarketcap.com/v1/cryptocurrency/categories \ --header 'X-CMC_PRO_API_KEY: YOUR_API_KEY'`

cURLJavaScriptPythonJavaGoC#KotlinObjective-CPHPRubySwift

Example Responses

200400401403429500

`{ "data": [ { "id": "605e2ce9d41eae1066535f7c", "name": "A16Z Portfolio", "title": "A16Z Portfolio", "description": "A16Z Portfolio", "num_tokens": 12, "avg_price_change": 0.61305157, "market_cap": 29429241867.031097, "market_cap_change": 3.049044106496, "volume": 4103706600.0391645, "volume_change": -10.538325849854, "last_updated": "2021-11-10T10:35:12.354Z" } ], "status": { "timestamp": "2021-08-01T22:51:28.209Z", "error_code": 0, "error_message": "", "elapsed": 3, "credit_count": 1 } }`

json

application/json

* * *

## Category[](#category)

GET

https://pro-api.coinmarketcap.com

/v1/cryptocurrency/category

Returns information about a single coin category available on CoinMarketCap. Includes a paginated list of the cryptocurrency quotes and metadata for the category.

**This endpoint is available on the following [API plans](https://coinmarketcap.com/api/pricing/):**

-   Free
-   Builder
-   Startup
-   Growth
-   Professional
-   Enterprise

Available with no API key

Call this endpoint keyless - no API key, no signup. Prefix the path with `/public-api`: `https://pro-api.coinmarketcap.com/public-api/v1/cryptocurrency/category`. See the [Keyless Public API](https://coinmarketcap.com/api/documentation/pro-api-reference/keyless-public-api) for the full list, rate limits, and examples.

**Cache / Update frequency:** Data is updated only as needed, every 30 seconds.  
**Plan credit use:** 1 API call credit per request + 1 call credit per 200 cryptocurrencies returned (rounded up) and 1 call credit per `convert` option beyond the first.  
**CMC equivalent pages:** Our Cryptocurrency Category page [coinmarketcap.com/cryptocurrency-category/](https://coinmarketcap.com/cryptocurrency-category/).

### Category › query Parameters[](#category/query-parameters)

`id`

​string · required

The Category ID. This can be found using the Categories API.

`start`

​integer · min: 1

Optionally offset the start (1-based index) of the paginated list of coins to return.

Default: 1

`limit`

​integer · min: 1 · max: 1000

Optionally specify the number of coins to return. Use this parameter and the "start" parameter to determine your own pagination size.

Default: 100

`convert`

​string · pattern: `^[0-9A-Za-z$@\-,]+(?…`

Optionally calculate market quotes in up to 120 currencies at once by passing a comma-separated list of cryptocurrency or fiat currency symbols. Each additional convert option beyond the first requires an additional call credit. A list of supported fiat options can be found [here](https://coinmarketcap.com/api/documentation/guides/standards-and-conventions). Each conversion is returned in its own "quote" object.

`convert_id`

​string · pattern: `^\d+(?:,\d+)*$`

Optionally calculate market quotes by CoinMarketCap ID instead of symbol. This option is identical to `convert` outside of ID format. Ex: convert\_id=1,2781 would replace convert=BTC,USD in your query. This parameter cannot be used when `convert` is used.

### Category › Headers[](#category/header-parameters)

`X-CMC_PRO_API_KEY`

​string · required

Your CoinMarketCap Pro API key

Default: YOUR\_API\_KEY

### Category › Responses[](#category/responses)

200400401403429500

Successful

[Category\_-\_Response\_Model](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#category-response-model)

`data`

​[Category\_-\_Results\_map](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#category-results-map) · required

Results of your query returned as an object map.

`status`

​[API\_Status\_Object](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#api-status-object)

Standardized status object for API calls.

GET/v1/cryptocurrency/category

`curl --request GET \ --url 'https://pro-api.coinmarketcap.com/v1/cryptocurrency/category?id=%3Cstring%3E' \ --header 'X-CMC_PRO_API_KEY: YOUR_API_KEY'`

cURLJavaScriptPythonJavaGoC#KotlinObjective-CPHPRubySwift

Example Responses

200400401403429500

`{ "data": { "1": { "id": "605e2ce9d41eae1066535f7c", "name": "A16Z Portfolio", "title": "A16Z Portfolio", "description": "A16Z Portfolio", "num_tokens": 12, "avg_price_change": 0.61305157, "market_cap": 29429241867.031097, "market_cap_change": 3.049044106496, "volume": 4103706600.0391645, "volume_change": -10.538325849854, "coins": [ { "id": 1, "name": "Bitcoin", "symbol": "BTC", "slug": "bitcoin", "cmc_rank": 5, "num_market_pairs": 500, "circulating_supply": 16950100, "total_supply": 16950100, "max_supply": 21000000, "last_updated": "2018-06-02T22:51:28.209Z", "date_added": "2013-04-28T00:00:00.000Z", "tags": [ "mineable" ], "platform": null, "quote": { "USD": { "price": 9283.92, "volume_24h": 7155680000, "percent_change_1h": -0.152774, "percent_change_24h": 0.518894, "percent_change_7d": 0.986573, "market_cap": 158055024432, "last_updated": "2018-08-09T22:53:32.000Z" } } }, { "id": 1027, "name": "Ethereum", "symbol": "ETH", "slug": "ethereum", "num_market_pairs": 6360, "circulating_supply": 16950100, "total_supply": 16950100, "max_supply": 21000000, "last_updated": "2018-06-02T22:51:28.209Z", "date_added": "2013-04-28T00:00:00.000Z", "tags": [ "mineable" ], "platform": null, "quote": { "USD": { "price": 1283.92, "volume_24h": 7155680000, "percent_change_1h": -0.152774, "percent_change_24h": 0.518894, "percent_change_7d": 0.986573, "market_cap": 158055024432, "last_updated": "2018-08-09T22:53:32.000Z" } } } ], "last_updated": "2021-11-10T10:35:12.354Z" } }, "status": { "timestamp": "2026-03-05T22:43:48.471Z", "error_code": 0, "error_message": "", "elapsed": 10, "credit_count": 1, "notice": "" } }`

json

application/json

* * *

## Airdrops[](#airdrops)

GET

https://pro-api.coinmarketcap.com

/v1/cryptocurrency/airdrops

Returns a list of past, present, or future airdrops which have run on CoinMarketCap.

**This endpoint is available on the following [API plans](https://coinmarketcap.com/api/pricing/):**

-   Builder
-   Startup
-   Growth
-   Professional
-   Enterprise

**Cache / Update frequency:** Data is updated only as needed, every 30 seconds.  
**Plan credit use:** 1 API call credit per request no matter query size.  
**CMC equivalent pages:** Our free airdrops page [coinmarketcap.com/airdrop/](https://coinmarketcap.com/airdrop/).

### Airdrops › query Parameters[](#airdrops/query-parameters)

`start`

​integer · min: 1

Optionally offset the start (1-based index) of the paginated list of items to return.

Default: 1

`limit`

​integer · min: 1 · max: 5000

Optionally specify the number of results to return. Use this parameter and the "start" parameter to determine your own pagination size.

Default: 100

`status`

​string · enum

What status of airdrops.

Enum values:

ENDED

ONGOING

UPCOMING

Default: ONGOING

`id`

​string · pattern: `^\d*$`

Filtered airdrops by one cryptocurrency CoinMarketCap IDs. Example: 1

`slug`

​string · pattern: `^[0-9a-z-]*$`

Alternatively filter airdrops by a cryptocurrency slug. Example: "bitcoin"

`symbol`

​string · pattern: `^[0-9A-Za-z$@\-]*$`

Alternatively filter airdrops one cryptocurrency symbol. Example: "BTC".

### Airdrops › Headers[](#airdrops/header-parameters)

`X-CMC_PRO_API_KEY`

​string · required

Your CoinMarketCap Pro API key

Default: YOUR\_API\_KEY

### Airdrops › Responses[](#airdrops/responses)

200400401403429500

Successful

[Airdrops\_-\_Response\_Model](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#airdrops-response-model)

`data`

​[Airdrops\_-\_Airdrop\_Object\[\]](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#airdrops-airdrop-object) · required

Array of airdrop object results.

`status`

​[API\_Status\_Object](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#api-status-object)

Standardized status object for API calls.

GET/v1/cryptocurrency/airdrops

`curl --request GET \ --url https://pro-api.coinmarketcap.com/v1/cryptocurrency/airdrops \ --header 'X-CMC_PRO_API_KEY: YOUR_API_KEY'`

cURLJavaScriptPythonJavaGoC#KotlinObjective-CPHPRubySwift

Example Responses

200400401403429500

`{ "data": [ { "id": "60e59b99c8ca1d58514a2322", "project_name": "DeRace Airdrop", "description": "For 7 days starting from August 15, 2021, CoinMarketCap will host an Airdrop event...", "status": "UPCOMING", "coin": { "id": 10744, "name": "DeRace", "slug": "derace", "symbol": "DERC" }, "start_date": "2021-06-01T22:11:00.000Z", "end_date": "2021-07-01T22:11:00.000Z", "total_prize": 20000000000, "winner_count": 1000, "link": "https://coinmarketcap.com/currencies/derace/airdrop/" } ], "status": { "timestamp": "2021-08-01T22:51:28.209Z", "error_code": 0, "error_message": "", "elapsed": 3, "credit_count": 1 } }`

json

application/json

* * *

## Airdrop[](#airdrop)

GET

https://pro-api.coinmarketcap.com

/v1/cryptocurrency/airdrop

Returns information about a single airdrop available on CoinMarketCap. Includes the cryptocurrency metadata.

**This endpoint is available on the following [API plans](https://coinmarketcap.com/api/pricing/):**

-   Builder
-   Startup
-   Growth
-   Professional
-   Enterprise

**Cache / Update frequency:** Data is updated only as needed, every 30 seconds.  
**Plan credit use:** 1 API call credit per request no matter query size.  
**CMC equivalent pages:** Our free airdrops page [coinmarketcap.com/airdrop/](https://coinmarketcap.com/airdrop/).

### Airdrop › query Parameters[](#airdrop/query-parameters)

`id`

​string · required

Airdrop Unique ID. This can be found using the Airdrops API.

### Airdrop › Headers[](#airdrop/header-parameters)

`X-CMC_PRO_API_KEY`

​string · required

Your CoinMarketCap Pro API key

Default: YOUR\_API\_KEY

### Airdrop › Responses[](#airdrop/responses)

200400401403429500

Successful

[Airdrop\_-\_Response\_Model](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#airdrop-response-model)

`data`

​[Airdrop\_-\_Results\_map](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#airdrop-results-map) · required

Results of your query returned as an object map.

`status`

​[API\_Status\_Object](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas#api-status-object)

Standardized status object for API calls.

GET/v1/cryptocurrency/airdrop

`curl --request GET \ --url 'https://pro-api.coinmarketcap.com/v1/cryptocurrency/airdrop?id=%3Cstring%3E' \ --header 'X-CMC_PRO_API_KEY: YOUR_API_KEY'`

cURLJavaScriptPythonJavaGoC#KotlinObjective-CPHPRubySwift

Example Responses

200400401403429500

`{ "data": { "1": { "id": "60e59b99c8ca1d58514a2322", "project_name": "DeRace Airdrop", "description": "For 7 days starting from August 15, 2021, CoinMarketCap will host an Airdrop event...", "status": "UPCOMING", "coin": { "id": 10744, "name": "DeRace", "slug": "derace", "symbol": "DERC" }, "start_date": "2021-06-01T22:11:00.000Z", "end_date": "2021-07-01T22:11:00.000Z", "total_prize": 20000000000, "winner_count": 1000, "link": "https://coinmarketcap.com/currencies/derace/airdrop/" } }, "status": { "timestamp": "2026-03-05T22:43:48.471Z", "error_code": 0, "error_message": "", "elapsed": 10, "credit_count": 1, "notice": "" } }`

json

application/json

* * *

[Choose an Endpoint](https://coinmarketcap.com/api/documentation/pro-api-reference/endpoint-overview)[Exchange](https://coinmarketcap.com/api/documentation/pro-api-reference/exchange)

---

*来源：[CoinMarketCap API Documentation](https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency)*