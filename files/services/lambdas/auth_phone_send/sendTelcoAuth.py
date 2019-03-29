#!/usr/bin/env python
# -*- coding: UTF-8 -*-

import os
import sys
import json
import random
import time

from httplib import BAD_REQUEST, INTERNAL_SERVER_ERROR, OK, GATEWAY_TIMEOUT

import boto3
import requests
from requests.exceptions import Timeout as TimeoutException

import phonenumbers

sys.path.append('/opt')

try:
    from wrapped_logging import log
except ImportError:
    print("failed importing wrapped_logging")
    import logging
    log = logging.getLogger("SendTelcoAuth")
except Exception, e:
    print ("Failed importing (no import error!): %s", e)
    import logging
    log = logging.getLogger("SendTelcoAuthNoAirbrake")

if os.environ.get("DEBUG"):
    import logging
    log.setLevel(logging.DEBUG)

idp = boto3.client('cognito-idp')


headers = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token',
    'Access-Control-Allow-Methods': 'POST,OPTIONS',
    'Access-Control-Allow-Credentials': True,
    'Content-Type': 'application/json'
}


def get_password():
    temp_password = get_mandatory_evar('TEMP_PASSWORD')
    # Reversing the temp password as actual password.
    password = temp_password[::-1]
    return password


# Getting the user attributes if the user already signed up.
def get_userinfo_by_phone(phoneno):
    idp_pool_id = get_mandatory_evar('COGNITO_IDP_POOL_ID')

    filter = "phone_number=\"{}\"".format(phoneno)

    response = idp.list_users(
        UserPoolId=idp_pool_id,
        Filter=filter
    )

    log.debug("List user response for filter {} is {}".format(filter, str(response)))

    if len(response['Users']) > 0:
        return response['Users'][0]
    return None


def user_phone_to_object(phonenumber, region=None):
    """
    Converts a user provided phone number to a phone object
    :param phonenumber:
    :return:
    """

    try:
        x = phonenumbers.parse(phonenumber)
    except phonenumbers.NumberParseException:
        log.warn("Failed to parse user provided number: %s", phonenumber)
    else:
        return x

    if not region:
        log.debug("Could not parse %s", phonenumber)
        return None

    log.debug("Parsing %s w/ region", phonenumber)
    try:
        x = phonenumbers.parse(phonenumber, region=region)
    except phonenumbers.NumberParseException:
        log.warn("Failed to parse user provided number w/ region")
    else:
        return x

    return None


def send(event, context):
    idp_pool_id = get_mandatory_evar('COGNITO_IDP_POOL_ID')
    idp_pool_client_id = get_mandatory_evar('COGNITO_IDP_POOL_CLIENT_ID')
    log.debug("Event %s", event)

    if 'body' not in event:
        return {
            'statusCode': BAD_REQUEST,
            'body': {
                'reason': "body"
            }
        }

    try:
        single_event = json.loads(event['body'])
    except TypeError:
        log.info("Failed loading body type=%s", type(event['body']))

        if isinstance(event['body'], dict):
            single_event = event['body']
        else:
            return {
                'statusCode': BAD_REQUEST,
                'body': {
                    'reason': 'Bad Body: \"%s\"' % event.get('body')
                }
            }

    original_phone_number = single_event.get('phonenumber')
    if not original_phone_number:
        return {
            'statusCode': BAD_REQUEST,
            'body': {
                'reason': "missing phonenumber"
            }
        }
    phone_object = user_phone_to_object(original_phone_number)
    if not phone_object:
        # TODO: Base country code off the country code of calling country, agent-terminal-id, key, length?
        phone_object = user_phone_to_object(original_phone_number, region='MX')

        if not phone_object:
            return {
                'statusCode': BAD_REQUEST,
                'headers': headers,
                'body': json.dumps({"reason": "Bad phonenumber: \"%s\"" % (
                    original_phone_number), })
        }
    phone_number = phonenumbers.format_number(phone_object, phonenumbers.PhoneNumberFormat.E164)

    #TODO: test the phone number:
    # https://stackoverflow.com/a/23299989/1257603
    # Also as an efficiency and performance measure: post to a queue and
    # do a bunch of messages at once
    auth_key = os.environ.get('PARAM_STORE_PRESHARED_KEY')
    log.debug("Retrieved Key: %s", auth_key)
    if not auth_key:
        return {
            'statusCode': INTERNAL_SERVER_ERROR,
            'body': {
                'reason': 'must set preshared key'
            }
        }

    api_path = os.environ.get('api_path', 'ops-alpha/telco/outbound')

    posting_path = "https://api.athenabitcoin.net/%s" % api_path


    # Querying the user info before sending the code so that we can reduce the delay between sms code and user creation.
    user_info = get_userinfo_by_phone(phone_number)

    rand_code = random.randint(100000, 999999)

    for lang in ['language', 'lang']:
        if lang in single_event:
            break
    default_lang_code = 'en' #provided in swagger
    lang_code = single_event.get(lang)

    countrycode = phonenumbers.region_code_for_number(phone_object)

    if not lang_code:
        if countrycode in ('MX', 'CO', 'AR'):
            lang_code = 'es'
        else:
            lang_code = default_lang_code

    if lang_code == 'en':
        msg = 'Your code is: %s' % rand_code
    elif lang_code == 'es':
        msg = 'Voy a enviarte un código: %s' % rand_code
    else:
        log.warn("Bad Language code, %s, defaulting to %s", lang_code, default_lang_code)

    data = {'phone_to': phone_number,
            'message': msg,
            'country': countrycode,
            'method': 'sms'}

    log.debug("Posting to data: %s to %s. Time Remaining: %s",
              data,
              posting_path,
              context.get_remaining_time_in_millis())

    try:
        z = requests.post(posting_path,
                          headers={'Authorization': auth_key},
                          json=data,
                          timeout=(context.get_remaining_time_in_millis() * .95 / 1000))
    except TimeoutException, te:
        return {
            'statusCode': GATEWAY_TIMEOUT ,
            'body': {
                'reason': 'Unexpected delay in processing'
            }
        }

    if not z.ok:
        log.error("%s failed with: %s", posting_path, z.text)
        return {
            'statusCode': z.status_code,
            'body': {
                'reason': 'Athena API: %s' % z.reason
            }
        }

    secret_info = json.dumps(
        {'code': rand_code,
         'ts': int(time.time() + time.timezone)})

    if user_info:
        '''
        If the user already present in user pool update the custom attribute with the code we sent and time.
        '''
        response = idp.admin_update_user_attributes(
            UserPoolId=idp_pool_id,
            Username=user_info['Username'],
            UserAttributes=[
                {
                    'Name': 'custom:otp_info',
                    'Value': secret_info
                },
            ]
        )
        log.debug("Custom Attribute update response:{}".format(response))
    else:
        '''
        if the user not present in our system create the unconfirmed user with the code we sent.
        '''
        response = idp.sign_up(
            ClientId=idp_pool_client_id,
            Username=str(phone_number),
            Password=get_password(),
            UserAttributes=[
                {
                    'Name':'phone_number',
                    'Value':str(phone_number)
                },
                {
                    'Name': 'custom:otp_info',
                    'Value': secret_info
                }
            ]
        )
        log.debug("Unconfirmed user creation response:{}".format(response))

    return {
        'statusCode': OK
        #'body': z
    }


def get_mandatory_evar(evar_name):
    if not evar_name in os.environ:
        raise ValueError("Missing environment variable: {}".format(evar_name))
    return os.environ[evar_name]


def get_mandatory_event_attr(event, attr):
    if not attr in event:
        raise ValueError("Missing event attribute: {}".format(attr))
    return event[attr]