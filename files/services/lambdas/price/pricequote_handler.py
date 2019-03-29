import json
import sys
from decimal import Decimal

import requests


#Append Opt for lambda layers
sys.path.append('/opt')

try:
    from wrapped_logging import log
except ImportError:
    print("failed importing wrapped_logging")
    import logging
    log = logging.getLogger("PriceQuote")
except Exception, e:
    print ("Failed importing (no import error!): %s", e)
    import logging
    log = logging.getLogger("PriceQuoteNoAirbrake")


class PriceRetrieveException(Exception):
    def __init__(self, reason):
        super(PriceRetrieveException, self).__init__(reason)


def handle_price_request(event, context):
    z = requests.get("https://markets.redleafadvisors.com/api/IMPLIED.BTCMXN", verify=False)
    markup = Decimal(.05)
    if z.ok:
        data = z.json()

        if 'last' in data:
            spot_price = Decimal(data['last'])
        elif all(map(lambda f: f in data, ('ask', 'bid'))):
            spot_price = Decimal((data['bid']+ data['ask'])/2)
        else:
            raise PriceRetrieveException(z.reason)
        body = json.dumps([
            {'deliveryCurrency': 'BTC',
             'paymentCurrency': 'MXN',
             'quotedPrice': "%.02f" % Decimal(spot_price * (markup + 1))}])

        return {
            'statusCode': 200,
            'body': body,
            'headers': {
                'Content-Type': 'application/json',
            }
        }
    else:
        print "Failed!!! %s" % z.reason
    raise PriceRetrieveException(z.reason)
