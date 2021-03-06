# -*- coding: utf-8 -*-
import logging
import random
from collections import namedtuple

from gevent.event import AsyncResult
from ethereum import slogging

from raiden.tasks import (
    StartMediatedTransferTask,
    MediateTransferTask,
    EndMediatedTransferTask,
    ExchangeTask,
)
from raiden.utils import pex

log = slogging.get_logger(__name__)  # pylint: disable=invalid-name
Exchange = namedtuple('Exchange', (
    'identifier',
    'from_token',
    'from_amount',
    'from_nodeaddress',  # the node' address of the owner of the `from_token`
    'to_token',
    'to_amount',
    'to_nodeaddress',  # the node' address of the owner of the `to_token`
))
ExchangeKey = namedtuple('ExchangeKey', (
    'from_token',
    'from_amount',
))


class UnknownAddress(Exception):
    pass


class TransferWhenClosed(Exception):
    pass


class UnknownTokenAddress(Exception):
    def __init__(self, address):
        self.address = address
        Exception.__init__(
            self,
            'Message with unknown token address {} received'.format(pex(address))
        )


class TransferManager(object):
    """ Manages all transfers done through this node. """

    def __init__(self, tokenmanager):
        self.tokenmanager = tokenmanager

        self.endtask_transfer_mapping = dict()

    def transfer_async(self, amount, target, identifier=None):
        """ Transfer `amount` between this node and `target`.

        This method will start an asyncronous transfer, the transfer might fail
        or succeed depending on a couple of factors:
            - Existence of a path that can be used, through the usage of direct
            or intermediary channels.
            - Network speed, making the transfer suficiently fast so it doesn't
            timeout.
        """
        direct_channel = self.tokenmanager.partneraddress_channel.get(target)

        if direct_channel:
            async_result = self._direct_or_mediated_transfer(
                amount,
                identifier,
                direct_channel,
            )
            return async_result

        else:
            async_result = self._mediated_transfer(
                amount,
                identifier,
                target,
            )

            return async_result

    def _direct_or_mediated_transfer(self, amount, identifier, direct_channel):
        """ Check the direct channel and if possible use it, otherwise start a
        mediated transfer.
        """

        if not direct_channel.isopen:
            log.info(
                'DIRECT CHANNEL %s > %s is closed',
                pex(direct_channel.our_state.address),
                pex(direct_channel.partner_state.address),
            )

            async_result = self._mediated_transfer(
                amount,
                identifier,
                direct_channel.partner_state.address,
            )
            return async_result

        elif amount > direct_channel.distributable:
            log.info(
                'DIRECT CHANNEL %s > %s doesnt have enough funds [%s]',
                pex(direct_channel.our_state.address),
                pex(direct_channel.partner_state.address),
                amount,
            )

            async_result = self._mediated_transfer(
                amount,
                identifier,
                direct_channel.partner_state.address,
            )
            return async_result

        else:
            direct_transfer = direct_channel.create_directtransfer(amount, identifier)
            self.tokenmanager.raiden.sign(direct_transfer)
            direct_channel.register_transfer(direct_transfer)

            async_result = self.tokenmanager.raiden.protocol.send_async(
                direct_channel.partner_state.address,
                direct_transfer,
            )
            return async_result

    def _mediated_transfer(self, amount, identifier, target):
        async_result = AsyncResult()
        task = StartMediatedTransferTask(
            self.tokenmanager.raiden,
            self.tokenmanager.token_address,
            amount,
            identifier,
            target,
            async_result,
        )
        task.start()

        return async_result
