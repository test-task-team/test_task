import os
import sys
import json
import time
import datetime

from distutils.util import strtobool

import boto3
import phonenumbers

# Append Opt for lambda layers
sys.path.append('/opt')

try:
    from wrapped_logging import log
except ImportError:
    print("failed importing wrapped_logging")
    import logging

    log = logging.getLogger("VerifyTelcoAuth")
except Exception, e:
    print ("Failed importing (no import error!): %s", e)
    import logging

    log = logging.getLogger("VerifyTelcoAuthNoAirbrake")

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
    'Access-Control-Allow-Methods': 'POST,OPTIONS',
    'Access-Control-Allow-Credentials': True,
    'Content-Type': 'application/json'
}

'''
Initializing boto3 clients
'''

idp = boto3.client('cognito-idp')

'''
Setting up the default time for validating token
'''

MAX_VALIDITY = 300

# to allow for testing
aws_profile = os.getenv('AWS_PROFILE')
if not aws_profile:
    dynamodb = boto3.resource('dynamodb')
    idp = boto3.client('cognito-idp')
else:
    sess = boto3.Session(profile_name=aws_profile)
    dynamodb = sess.resource('dynamodb')
    idp = sess.client('cognito-idp')


def get_password():
    temp_password = get_mandatory_evar('TEMP_PASSWORD')
    # Reversing the temp password as actual password.
    password = temp_password[::-1]
    return password


# Getting the user attributes if the user already signedup.
def get_userinfo_by_phone(phoneno):
    idp_pool_id = get_mandatory_evar('COGNITO_IDP_POOL_ID')

    filter = "phone_number=\"{}\"".format(phoneno)

    try:
        response = idp.list_users(
            UserPoolId=idp_pool_id,
            Filter=filter)
    except Exception, e:
        log.exception("Failed looking up list user (type %s)" % type(e))
        return None

    log.debug("List user response for filter {} is {}".format(filter, str(response)))

    if response.get('ResponseMetadata', {}).get('HTTPStatusCode') not in (200, 201, 202):
        log.error("Failed to look up user successfully")
        return None

    users = response.get('Users')
    if not users:
        log.error("Failed retrieving users: %s", response)
        return None

    if len(users) == 0:
        log.error("No users found")
        return None

    if len(users) > 1:
        log.warn("More than one user in %s for %s (taking the first): %s", idp_pool_id,
                 phoneno, users)

    return users[0]


def authenticate_or_confirm_user(user_info):
    """
    this method takes care of confiriming the users and nullifying the otp_info custom attribute
    :param user_info:
    :return:
    """
    idp_pool_id = get_mandatory_evar('COGNITO_IDP_POOL_ID')
    log.debug("Checking the user confirmation status")
    if user_info['UserStatus'] == 'UNCONFIRMED':
        log.info("Confirming the user: {} ".format(user_info['Username']))
        response = idp.admin_confirm_sign_up(
            UserPoolId=idp_pool_id,
            Username=user_info['Username']
        )
        log.info("Signup confirm response: {} ".format(response))

    # TODO: How do we mark the phone as validated?
    #  https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/cognito-idp.html#CognitoIdentityProvider.Client.verify_user_attribute
    # resetting the otp_info custom attribute
    response = idp.admin_update_user_attributes(
        UserPoolId=idp_pool_id,
        Username=user_info['Username'],
        UserAttributes=[
            {
                'Name': 'custom:otp_info',
                'Value': ''
            }
        ]
    )

    log.debug("Custom Attribute update response:{}".format(response))


# TODO: Search Using user attributes to check if the user already created.
def get_access_token(user_name):
    """
    this method logs in as the user using standard password and generates the jwt tokens
    :param user_name:
    :return:
    """
    idp_pool_id = get_mandatory_evar('COGNITO_IDP_POOL_ID')

    idp_pool_client_id = get_mandatory_evar('COGNITO_IDP_POOL_CLIENT_ID')

    password = get_password()

    auth_response = idp.admin_initiate_auth(
        UserPoolId=idp_pool_id,
        ClientId=idp_pool_client_id,
        AuthFlow='ADMIN_NO_SRP_AUTH',
        AuthParameters={
            'USERNAME': user_name,
            'PASSWORD': password
        })
    log.debug(" Admin initiate auth is successful for user {} ".format(user_name))
    return auth_response['AuthenticationResult']


def default_encode(o):
    if isinstance(o, (datetime.date, datetime.datetime)):
        return o.isoformat()
    # elif isinstance(o, Decimal):
    # return "%.08" % o


def codes_match(codeA, codeB):
    if codeA == codeB:
        return True

    if isinstance(codeA, basestring) and codeA.isdigit() and isinstance(codeB, int):
        return int(codeA) == codeB

    if isinstance(codeB, basestring) and codeB.isdigit() and isinstance(codeA, int):
        return codeA == int(codeB)

    return False


def user_phone_to_object(phonenumber, region=None):
    """
    Converts a user provided phone number to a phone object
    :param phonenumber:
    :return:
    """

    try:
        x = phonenumbers.parse(phonenumber)
    except phonenumbers.NumberParseException:
        log.warn("Failed to parse user provided number")
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


def user_phone_to_e164(phonenumber):
    """
    Converts a phone or string
    """
    obj = user_phone_to_object(phonenumber, 'MX')

    if not obj:
        return None

    return phonenumbers.format_number(obj, phonenumbers.PhoneNumberFormat.E164)


def verify(event, context):
    log.debug("Received event: %s", json.dumps(event))

    try:
        event = json.loads(event["body"])
    except ValueError:
        return {
            'statusCode': 403,
            'headers': headers,
            'body': json.dumps({"reason": "Bad json in body"})
        }

    try:
        code = get_mandatory_event_attr(event, 'code')
        original_phone_number = get_mandatory_event_attr(event, 'phonenumber')
    except ValueError, ve:
        log.error("Missing Code or phonenumber")
        return {
            'statusCode': 403,
            'headers': headers,
            'body': json.dumps({"reason": "Missing code or phonenumber"})
        }

    phone_number = user_phone_to_e164(original_phone_number)
    if not phone_number:
        return {
            'statusCode': 403,
            'headers': headers,
            'body': json.dumps({"reason": "Bad phonenumber: \"%s\"" % (
                original_phone_number), })
        }

    user_info = get_userinfo_by_phone(phone_number)

    if user_info is None:
        log.info("Failed looking up user %s", phone_number)
        return {
            'statusCode': 403,
            'headers': headers,
            'body': json.dumps({"reason": "Invalid Mobile or OTP!"})
        }

    # validating the secret code block
    otp_verified = False

    for attr in user_info['Attributes']:
        if attr['Name'] != 'custom:otp_info':
            log.debug("Skipping user attribute: %s", attr['Name'])
            continue
        try:
            otp_info = json.loads(attr['Value'])
        except ValueError, ve:
            log.error("Could not decode data in custom:otp_info: %s", attr['Value'])
            continue

        if 'code' not in otp_info:
            log.warn("Bad format of hidden code")
            continue

        if not codes_match(code, otp_info['code']):
            continue

        saved_ts = otp_info.get('ts')
        if saved_ts:
            code_age = datetime.timedelta(seconds=time.time() + time.timezone - saved_ts)
            if code_age > datetime.timedelta(minutes=15):
                log.info("Code confirmation took too long: %s", code_age)
                continue
        else:
            log.warn("Bad format of hidden code (no ts)")
            code_age = 'NA'
        authenticate_or_confirm_user(user_info)
        otp_verified = True
        log.info("user %s confirmed after %s!!", user_info, code_age)

    if not otp_verified:
        return {
            'statusCode': 403,
            'headers': headers,
            'body': json.dumps({"reason": "Invalid Mobile or OTP!"})
        }

    # generating access token for authenticated user.
    return {
        'statusCode': 200,
        'headers': headers,
        'body': json.dumps(
            phone_to_auth_response(user_info),
            default=default_encode
        )
    }


def id_from_userinfo(u):
    for a in u.get('Attributes', []):
        if not a.get('Name') == 'sub':
            continue
        return a['Value']
    log.warn("Could not get an id for %s", u)
    return None


def phone_to_auth_response(user_info):
    """
    :param phoneno:
    :return:
    """

    accesstoken = get_access_token(user_info['Username'])

    # TODO:
    #  -- Has this user exceeded their daily limits?
    #  -- Has this user submitted an id / been verified

    return {
        "customer_id": id_from_userinfo(user_info),
        "authorized": True,
        "buy_limit": 500,
        "sell_limit": 500,
        "user_info": user_info['Attributes'],
        "authorization_token": accesstoken
    }


def get_mandatory_evar(evar_name):
    if not evar_name in os.environ:
        raise ValueError("Missing environment variable: {}".format(evar_name))
    return os.environ[evar_name]


def get_mandatory_event_attr(event, attr):
    if not attr in event:
        raise ValueError("Missing event attribute: {}".format(attr))
    return event[attr]
