import json
import sys

original_except_hook = sys.excepthook

import os
import datetime
from decimal import Decimal, InvalidOperation
from httplib import BAD_REQUEST, OK, INTERNAL_SERVER_ERROR
from distutils.util import strtobool

from pycoin.key import validate as pycoin_validator

#Append Opt for lambda layers
sys.path.append('/opt')

try:
    from wrapped_logging import log
except ImportError:
    print("failed importing wrapped_logging")
    import logging
    log = logging.getLogger("Transaction")
except Exception, e:
    print ("Failed importing (no import error!): %s", e)

    import logging
    log = logging.getLogger("TransactionNoAirbrake")

from purchase_object import QPagosPurchase, TxInstantiationException


try:
    debug_on = strtobool(os.environ.get("DEBUG"))
except:
    log.error("Fix the debug param")
    debug_on = True

if debug_on:
    import logging
    log.setLevel(logging.DEBUG)


headers = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token',
    'Access-Control-Allow-Methods': 'POST',
    'Access-Control-Allow-Credentials': True,
    'Content-Type': 'application/json'
}


def default_encode(o):
    """
    # TODO --> This should move to a lambda-utils layer
    :param o:
    :return:
    """
    if isinstance(o, (datetime.date, datetime.datetime)):
        return o.isoformat()
    elif isinstance(o, Decimal):
        return "%.08f" % o


def create_transaction(event, context):
    """

    :param event:
    :param context:
    :return:
    """
    log.debug("Received event in Create: %s", event)
    request_body = event['body']
    if isinstance(request_body, basestring):
        try:
            request_body = json.loads(request_body)
        except TypeError:
            return {
                'statusCode': BAD_REQUEST,
                'headers': headers,
                'body': json.dumps({
                    'message': 'Cannot decode JSON'
                })}
        except ValueError:
            return {
                'statusCode': BAD_REQUEST,
                'headers': headers,
                'body': json.dumps({
                    'message': 'Bad request (no body)'
                })}
    elif isinstance(request_body, dict):
        log.debug("Body type was dict; assuming test request")
        pass
    else:
        return {
            'statusCode': BAD_REQUEST,
            'headers': headers,
            'body': json.dumps({
                'message': "Bad type for body: %s" % type(request_body)
            })}

    if "address" not in request_body:
        return {
            'statusCode': BAD_REQUEST,
            'headers': headers,
            'body': json.dumps({
                'message': 'No Address'
            })}

    is_good_address = pycoin_validator.is_address_valid(address=request_body['address'])
    if not is_good_address:
        return {
            'statusCode': BAD_REQUEST,
            'headers': headers,
            'body': json.dumps({
                'message': 'Not a valid address: %s' % request_body['address']
            })}

    try:
        tx = QPagosPurchase(initial="price_quoted",
                            request_context=event['requestContext'],
                            **request_body)
    except TxInstantiationException, tie:
        return {
            'statusCode': BAD_REQUEST,
            'headers': headers,
            'body': tie.to_json()
        }

    tx.request_sub = event['requestContext']['authorizer']['claims']['sub']
    tx.request_ip = event['requestContext']['identity']['sourceIp']

    # This kludge about except hooks is in order to be able to pickle.
    #   We were getting errors about not being able to pickle because of some
    #   excepthook type error that I could not figure out in a timely manner
    airbrake_except_hook = sys.excepthook
    sys.excepthook = original_except_hook
    try:
        tx.submit()
    finally:
        sys.excepthook = airbrake_except_hook

    tx_as_dict = tx.as_dict()

    try:
        tx_json = json.dumps(tx_as_dict, default=default_encode)
    except (TypeError, ValueError), e:
        log.exception("Failed to create tx as json")
        try:
            log.debug("Dictionary failed to convert: %s", tx_as_dict)
        except:
            log.warn("Could not even convert to dict")
        finally:
            return {'statusCode': INTERNAL_SERVER_ERROR,
                    'body': {
                        "message": "Object failed to serialize to json (%s)" % e
                    },
                    'headers': headers}
    else:
        return {'statusCode': OK,
                'body': tx_json,
                'headers': headers}


def complete_transaction(event, context):
    """

    :param event:
    :param context:
    :return:
    """
    # TODO --> this is a copy.  We should wrap these in a decorator & reduce the code.
    log.debug("Received event in Complete: %s", event)
    request_body = event['body']
    if isinstance(request_body, basestring):
        try:
            request_body = json.loads(request_body)
        except TypeError:
            return {
                'statusCode': BAD_REQUEST,
                'headers': headers,
                'body': json.dumps({
                    'message': 'Cannot decode JSON'
                })}
        except ValueError:
            return {
                'statusCode': BAD_REQUEST,
                'headers': headers,
                'body': json.dumps({
                    'message': 'Bad request (no body)'
                })}
    elif isinstance(request_body, dict):
        log.debug("Body type was dict; assuming test request")
        pass
    else:
        return {
            'statusCode': BAD_REQUEST,
            'headers': headers,
            'body': json.dumps({
                'message': "Bad type for body: %s" % type(request_body)
            })}
    #### Copied over

    required_fields = ['uuid', "amount_deposited"]
    for f in required_fields:
        if f in request_body:
            continue

        return {
            'statusCode': BAD_REQUEST,
            'headers': headers,
            'body': json.dumps({
                'message': "Request needs %s" % f
            })}
    storage = QPagosPurchase.get_storage()
    persistent_key = request_body['uuid']
    log.debug("Looking for key=%s in %s", persistent_key, storage)
    try:
        order = storage.retrieve(persistent_key)
    except Exception, e:
        log.exception("Failed retrieving %s from %s", persistent_key, storage)
        return {
            'statusCode': BAD_REQUEST,
            'headers': headers,
            'body': json.dumps({
                'message': str(e)
            })}
    else:
        log.debug("Retrieved %s from storage", order)

    if not order:
        return {
            'statusCode': BAD_REQUEST,
            'headers': headers,
            'body': json.dumps({
                'message': "Failed retrieving order %s" % persistent_key
            })}

    try:
        deposited = Decimal(request_body['amount_deposited'])
    except InvalidOperation:
        return {
            'statusCode': BAD_REQUEST,
            'headers': headers,
            'body': json.dumps({
                'message': "Bad deposit amount: %s" % request_body['amount_deposited']
            })}

    order.amount_paid += getattr(order, 'amount_paid', Decimal(0))
    order.confirm_payment()

    return {'statusCode': OK,
            'body': order.as_dict(),
            'headers': headers
            }
