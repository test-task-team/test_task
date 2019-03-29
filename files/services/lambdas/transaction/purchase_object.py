import os
import sys
import logging

import boto3

log = logging.getLogger("objects")

#Append Opt for lambda layers
sys.path.append('/opt')

from decimal import InvalidOperation, Decimal

log.debug("Importing Purchase Request")
from apsm.purchase import PurchaseRequest
log.debug("Imported Purchase Request")

from apsm.machine_storage import DynamoStore

dynamoResource = boto3.resource('dynamodb')


class TxInstantiationException(Exception):

    def __init__(self, *args, **kwargs):
        super(TxInstantiationException, self).__init__(**kwargs)

    def to_json(self):
        raise NotImplementedError()


class QPagosPurchase(PurchaseRequest):
    """


    """

    @staticmethod
    def get_storage():
        if not 'TRANSACTION_TABLE' in os.environ:
            raise ValueError("Missing environment variable: {}".format('TRANSACTION_TABLE'))
        table_name = os.environ['TRANSACTION_TABLE']
        return DynamoStore(table=dynamoResource.Table(table_name))

    def __init__(self,
                 user_pool=None,
                 cryptoCurrency=None, fiat=None, side=None,
                 amount=None, *args, **kwargs):
        """
        Given a dictionary, we will make a transaction out of it.  If we fail to
        do it, we'll raise an AttributeError (missing), TypeError (string for int
        or vice versa)
        """

        if None in (cryptoCurrency, fiat, side, amount):
            raise TxInstantiationException("Missing required field")

        self.cryptoCurrency = cryptoCurrency
        if self.cryptoCurrency not in ("BTC", "LTC", "BCH"):
            raise TxInstantiationException("Unsupported crypto \"%s\"" % (
                self.cryptoCurrency,))

        self.fiat = fiat
        if self.fiat not in ('USD', 'MXN'):
            raise TxInstantiationException("Unsupported currency \"%s\"" % (
                self.fiat))

        try:
            self.amount = Decimal(amount)
        except InvalidOperation:
            raise TxInstantiationException("Unsupported amount format \"%s\"" % (
                self.amount))

        self.side = side
        if self.side not in ('Buy', 'Sell'):
            raise TxInstantiationException("Unsupported side \"%s\"" % (
                self.side))

        if self.side == 'Buy':
            self.address = kwargs.get('address')
            if not self.address:
                raise TxInstantiationException("Missing Address side \"%s\"" % (
                    self.fiat))
        else:
            raise TxInstantiationException("Sell currently not supported")

        persist_helper = QPagosPurchase.get_storage()
        super(QPagosPurchase, self).__init__(loader=persist_helper,
                                             persist_store=persist_helper,
                                             persistent_id_field='uuid',
                                             initial=kwargs.get('initial'))

    @staticmethod
    def get_repr_fields():
        return ['fiat', 'address', 'side', 'cryptoCurrency', 'amount']

    def __repr__(self):
        return "UUID: %s; State: %s" % (self.persistent_id,
                                        self.state)