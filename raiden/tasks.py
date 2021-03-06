# -*- coding: utf-8 -*-
# pylint: disable=too-many-lines
import logging
import random
import time

import gevent
from gevent.event import AsyncResult
from gevent.queue import Empty, Queue
from gevent.timeout import Timeout
from ethereum import slogging
from ethereum.utils import sha3

from raiden.messages import (
    MediatedTransfer,
    RefundTransfer,
    RevealSecret,
    Secret,
    SecretRequest,
)
from raiden.settings import (
    DEFAULT_HEALTHCHECK_POLL_TIMEOUT,
    DEFAULT_EVENTS_POLL_TIMEOUT,
    ESTIMATED_BLOCK_TIME,
)
from raiden.utils import lpex, pex

__all__ = (
    'StartMediatedTransferTask',
    'MediateTransferTask',
    'EndMediatedTransferTask',
)

log = slogging.get_logger(__name__)  # pylint: disable=invalid-name
REMOVE_CALLBACK = object()
TIMEOUT = object()


class Task(gevent.Greenlet):
    """ Base class used to created tasks.

    Note:
        Always call super().__init__().
    """

    def __init__(self):
        super(Task, self).__init__()
        self.response_queue = Queue()

    def on_response(self, response):
        """ Add a new response message to the task queue. """
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                'RESPONSE MESSAGE RECEIVED %s %s',
                repr(self),
                response,
            )

        self.response_queue.put(response)


class HealthcheckTask(Task):
    """ Task for checking if all of our open channels are healthy """

    def __init__(
            self,
            raiden,
            send_ping_time,
            max_unresponsive_time,
            sleep_time=DEFAULT_HEALTHCHECK_POLL_TIMEOUT):
        """ Initialize a HealthcheckTask that will monitor open channels for
             responsiveness.

             :param raiden RaidenService: The Raiden service which will give us
                                          access to the protocol object and to
                                          the token manager
             :param int sleep_time: Time in seconds between each healthcheck task
             :param int send_ping_time: Time in seconds after not having received
                                        a message from an address at which to send
                                        a Ping.
             :param int max_unresponsive_time: Time in seconds after not having received
                                               a message from an address at which it
                                               should be deleted.
         """
        super(HealthcheckTask, self).__init__()

        self.protocol = raiden.protocol
        self.raiden = raiden

        self.stop_event = AsyncResult()
        self.sleep_time = sleep_time
        self.send_ping_time = send_ping_time
        self.max_unresponsive_time = max_unresponsive_time
        self.timeout = None

    def _run(self):  # pylint: disable=method-hidden
        stop = None
        while stop is None:
            keys_to_remove = []
            for key, queue in self.protocol.address_queue.iteritems():
                receiver_address = key[0]
                token_address = key[1]
                if queue.empty():
                    elapsed_time = (
                        time.time() - self.protocol.last_received_time[receiver_address]
                    )
                    # Add a randomized delay in the loop to not clog the network
                    gevent.sleep(random.randint(0, int(0.2 * self.send_ping_time)))
                    if elapsed_time > self.max_unresponsive_time:
                        graph = self.raiden.channelgraphs[token_address]
                        graph.remove_path(self.protocol.raiden.address, receiver_address)
                        # remove the node from the queue
                        keys_to_remove.append(key)
                    elif elapsed_time > self.send_ping_time:
                        self.protocol.send_ping(receiver_address)

            for key in keys_to_remove:
                self.protocol.address_queue.pop(key)

            self.timeout = Timeout(self.sleep_time)  # wait() will call cancel()
            stop = self.stop_event.wait(self.timeout)

    def stop_and_wait(self):
        self.stop_event.set(True)
        gevent.wait(self)

    def stop_async(self):
        self.stop_event.set(True)


class AlarmTask(Task):
    """ Task to notify when a block is mined. """

    def __init__(self, chain):
        super(AlarmTask, self).__init__()

        self.callbacks = list()
        self.stop_event = AsyncResult()
        self.chain = chain
        self.last_block_number = self.chain.block_number()

        # TODO: Start with a larger wait_time and decrease it as the
        # probability of a new block increases.
        self.wait_time = 0.5

    def register_callback(self, callback):
        """ Register a new callback.

        Note:
            This callback will be executed in the AlarmTask context and for
            this reason it should not block, otherwise we can miss block
            changes.
        """
        if not callable(callback):
            raise ValueError('callback is not a callable')

        self.callbacks.append(callback)

    def _run(self):  # pylint: disable=method-hidden
        stop = None
        result = None
        last_loop = time.time()
        log.debug('starting block number', block_number=self.last_block_number)

        while stop is None:
            current_block = self.chain.block_number()

            if current_block > self.last_block_number + 1:
                difference = current_block - self.last_block_number - 1
                log.error(
                    'alarm missed %s blocks',
                    difference,
                )

            if current_block != self.last_block_number:
                self.last_block_number = current_block
                log.debug('new block', number=current_block, timestamp=last_loop)

                remove = list()
                for callback in self.callbacks:
                    try:
                        result = callback(current_block)
                    except:  # pylint: disable=bare-except
                        log.exception('unexpected exception on alarm')
                    else:
                        if result is REMOVE_CALLBACK:
                            remove.append(callback)

                for callback in remove:
                    self.callbacks.remove(callback)

            # we want this task to iterate in the tick of `wait_time`, so take
            # into account how long we spent executing one tick.
            work_time = time.time() - last_loop
            if work_time > self.wait_time:
                log.warning(
                    'alarm loop is taking longer than the wait time',
                    work_time=work_time,
                    wait_time=self.wait_time,
                )
                sleep_time = 0.001
            else:
                sleep_time = self.wait_time - work_time

            stop = self.stop_event.wait(sleep_time)
            last_loop = time.time()

    def stop_and_wait(self):
        self.stop_event.set(True)
        gevent.wait(self)

    def stop_async(self):
        self.stop_event.set(True)


class BaseMediatedTransferTask(Task):
    def _send_and_wait_time(self, raiden, recipient, transfer, timeout):
        """ Utility to handle multiple messages for the same hashlock while
        properly handling expiration timeouts.
        """

        current_time = time.time()
        limit_time = current_time + timeout

        raiden.send_async(recipient, transfer)

        while current_time <= limit_time:
            # wait for a response message (not the Ack for the transfer)
            try:
                response = self.response_queue.get(
                    timeout=limit_time - current_time,
                )
            except Empty:
                yield TIMEOUT
                return

            yield response

            current_time = time.time()

        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                'TIMED OUT %s %s',
                self.__class__,
                pex(transfer),
            )

    def _send_and_wait_block(self, raiden, recipient, transfer, expiration_block):
        """ Utility to handle multiple messages and timeout on a blocknumber. """
        raiden.send_async(recipient, transfer)

        current_block = raiden.get_block_number()
        while current_block < expiration_block:
            try:
                response = self.response_queue.get(
                    timeout=DEFAULT_EVENTS_POLL_TIMEOUT,
                )
            except Empty:
                pass
            else:
                if response:
                    yield response

            current_block = raiden.get_block_number()

        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                'TIMED OUT ON BLOCK %s %s %s',
                current_block,
                self.__class__,
                pex(transfer),
            )

        yield TIMEOUT

    def _wait_for_unlock_or_close(self, raiden, graph, channel, mediated_transfer):  # noqa
        """ Wait for a Secret message from our partner to update the local
        state, if the Secret message is not sent within time the channel will
        be closed.

        Note:
            Must be called only once the secret is known.
            Must call `on_hashlock_result` after this function returns.
        """
        assert graph.token_address == mediated_transfer.token

        if not isinstance(mediated_transfer, MediatedTransfer):
            raise ValueError('MediatedTransfer expected.')

        block_to_close = mediated_transfer.lock.expiration - raiden.config['reveal_timeout']
        hashlock = mediated_transfer.lock.hashlock
        identifier = mediated_transfer.identifier
        token = mediated_transfer.token

        while channel.our_state.balance_proof.is_unclaimed(hashlock):
            current_block = raiden.get_block_number()

            if current_block > block_to_close:
                if log.isEnabledFor(logging.WARN):
                    log.warn(
                        'Closing channel (%s, %s) to prevent expiration of lock %s %s',
                        pex(channel.our_state.address),
                        pex(channel.partner_state.address),
                        pex(hashlock),
                        repr(self),
                    )

                channel.netting_channel.close(
                    channel.our_state.address,
                    channel.our_state.balance_proof.transfer,
                    channel.partner_state.balance_proof.transfer,
                )
                return

            try:
                response = self.response_queue.get(
                    timeout=DEFAULT_EVENTS_POLL_TIMEOUT
                )
            except Empty:
                pass
            else:
                if isinstance(response, Secret):
                    secret = response.secret
                    hashlock = sha3(secret)

                    if response.identifier == identifier and response.token == token:
                        raiden.handle_secret(
                            identifier,
                            graph.token_address,
                            secret,
                            response,
                            hashlock,
                        )
                    else:
                        # cannot use the message but the secret is okay
                        raiden.handle_secret(
                            identifier,
                            graph.token_address,
                            secret,
                            None,
                            hashlock,
                        )

                        if log.isEnabledFor(logging.ERROR):
                            log.error(
                                'Invalid Secret message received, expected message'
                                ' for token=%s identifier=%s received=%s',
                                token,
                                identifier,
                                response,
                            )

                elif isinstance(response, RevealSecret):
                    secret = response.secret
                    hashlock = sha3(secret)
                    raiden.handle_secret(
                        identifier,
                        graph.token_address,
                        secret,
                        None,
                        hashlock,
                    )

                elif log.isEnabledFor(logging.ERROR):
                    log.error(
                        'Invalid message ignoring. %s %s',
                        repr(response),
                        repr(self),
                    )

    def _wait_expiration(self, raiden, transfer, sleep=DEFAULT_EVENTS_POLL_TIMEOUT):
        """ Utility to wait until the expiration block.

        For a chain A-B-C, if an attacker controls A and C a mediated transfer
        can be done through B and C will wait for/send a timeout, for that
        reason B must not unregister the hashlock until the lock has expired,
        otherwise the revealed secret wouldn't be caught.
        """
        # pylint: disable=no-self-use

        expiration = transfer.lock.expiration + 1

        while True:
            current_block = raiden.get_block_number()

            if current_block > expiration:
                return

            gevent.sleep(sleep)


# Note: send_and_wait_valid methods are used to check the message type and
# sender only, this can be improved by using a encrypted connection between the
# nodes making the signature validation unnecessary


# TODO:
# - implement the token swap as a restartable task
# - add a separate message for exchanges to simplify message handling
class StartExchangeTask(BaseMediatedTransferTask):
    """ Initiator task, responsible to choose a random secret, initiate the
    token exchange by sending a mediated transfer to the counterparty and
    revealing the secret once the exchange can be complete.
    """

    def __init__(self, identifier, raiden, from_token, from_amount, to_token, to_amount, target):
        # pylint: disable=too-many-arguments

        super(StartExchangeTask, self).__init__()

        self.identifier = identifier
        self.raiden = raiden
        self.from_token = from_token
        self.from_amount = from_amount
        self.to_token = to_token
        self.to_amount = to_amount
        self.target = target

    def __repr__(self):
        return '<{} {} from_token:{} to_token:{}>'.format(
            self.__class__.__name__,
            pex(self.raiden.address),
            pex(self.from_token),
            pex(self.to_token),
        )

    def _run(self):  # pylint: disable=method-hidden,too-many-locals
        identifier = self.identifier
        raiden = self.raiden
        from_token = self.from_token
        from_amount = self.from_amount
        to_token = self.to_token
        to_amount = self.to_amount
        target = self.target

        from_graph = raiden.channelgraphs[from_token]
        to_graph = raiden.channelgraphs[to_token]

        from_routes = from_graph.get_best_routes(
            raiden.address,
            target,
            from_amount,
            lock_timeout=None,
        )
        fee = 0

        for path, from_channel in from_routes:
            # for each new path a new secret must be used
            secret = sha3(hex(random.getrandbits(256)))
            hashlock = sha3(secret)

            raiden.register_task_for_hashlock(self, from_token, hashlock)
            raiden.register_channel_for_hashlock(from_token, from_channel, hashlock)

            lock_expiration = (
                raiden.get_block_number() +
                from_channel.settle_timeout -
                raiden.config['reveal_timeout']
            )

            from_mediated_transfer = from_channel.create_mediatedtransfer(
                raiden.address,
                target,
                fee,
                from_amount,
                identifier,
                lock_expiration,
                hashlock,
            )
            raiden.sign(from_mediated_transfer)
            from_channel.register_transfer(from_mediated_transfer)

            # wait for the SecretRequest and MediatedTransfer
            to_mediated_transfer = self.send_and_wait_valid_state(
                raiden,
                path,
                from_mediated_transfer,
                to_token,
                to_amount,
            )

            if to_mediated_transfer is None:
                # the initiator can unregister right away since it knows the
                # secret wont be revealed
                raiden.on_hashlock_result(from_token, hashlock, False)

            elif isinstance(to_mediated_transfer, MediatedTransfer):
                to_hop = to_mediated_transfer.sender

                # reveal the secret to the `to_hop` and `target`
                self.reveal_secret(
                    self.raiden,
                    secret,
                    last_node=to_hop,
                    exchange_node=target,
                )

                to_channel = to_graph.partneraddress_channel[to_mediated_transfer.sender]

                raiden.handle_secret(
                    identifier,
                    to_token,
                    secret,
                    None,
                    hashlock,
                )

                raiden.handle_secret(
                    identifier,
                    from_token,
                    secret,
                    None,
                    hashlock,
                )

                self._wait_for_unlock_or_close(
                    raiden,
                    to_graph,
                    to_channel,
                    to_mediated_transfer,
                )

                raiden.on_hashlock_result(from_token, hashlock, True)
                self.done_result.set(True)

    def send_and_wait_valid_state(  # noqa
            self,
            raiden,
            path,
            from_token_transfer,
            to_token,
            to_amount):
        """ Start the exchange by sending the first mediated transfer to the
        taker and wait for mediated transfer for the exchanged token.

        This method will validate the messages received, discard the invalid
        ones, and wait until a valid state is reached. The valid state is
        reached when a mediated transfer for `to_token` with `to_amount` tokens
        and a SecretRequest from the taker are received.

        Returns:
            None: when the timeout was reached.
            MediatedTransfer: when a valid state is reached.
            RefundTransfer: when an invalid state is reached by
                our partner.
        """
        # pylint: disable=too-many-arguments

        next_hop = path[1]
        taker_address = path[-1]  # taker_address and next_hop might be equal

        # a valid state must have a secret request from the maker and a valid
        # mediated transfer for the new token
        received_secretrequest = False
        mediated_transfer = None

        response_iterator = self._send_and_wait_time(
            raiden,
            from_token_transfer.recipient,
            from_token_transfer,
            raiden.config['msg_timeout'],
        )

        for response in response_iterator:

            if response is None:
                if log.isEnabledFor(logging.DEBUG):
                    log.debug(
                        'EXCHANGE TRANSFER TIMED OUT hashlock:%s',
                        pex(from_token_transfer.lock.hashlock),
                    )

                return None

            # The MediatedTransfer might be from `next_hop` or most likely from
            # a different node.
            #
            # The other participant must not use a direct transfer to finish
            # the token exchange, ignore it
            if isinstance(response, MediatedTransfer) and response.token == to_token:
                # XXX: allow multiple transfers to add up to the correct amount
                if response.lock.amount == to_amount:
                    mediated_transfer = response

            elif isinstance(response, SecretRequest) and response.sender == taker_address:
                received_secretrequest = True

            # next_hop could send the MediatedTransfer, this is handled in a
            # previous if
            elif response.sender == next_hop:
                if isinstance(response, RefundTransfer):
                    return response
                else:
                    if log.isEnabledFor(logging.INFO):
                        log.info(
                            'Partner %s sent an invalid message %s',
                            pex(next_hop),
                            repr(response),
                        )

                    return None

            elif log.isEnabledFor(logging.ERROR):
                log.error(
                    'Invalid message ignoring. %s',
                    repr(response),
                )

            if mediated_transfer and received_secretrequest:
                return mediated_transfer

        return None

    def reveal_secret(self, raiden, secret, last_node, exchange_node):
        """ Reveal the `secret` to both participants.

        The secret must be revealed backwards to get the incentives right
        (first mediator would not forward the secret and get the transfer to
        itself).

        With exchanges there is an additional failure point, if a node is
        mediating both token transfers it can intercept the transfer (as in not
        revealing the secret to others), for this reason it is not sufficient
        to just send the Secret backwards, the Secret must also be sent to the
        exchange_node.
        """
        # pylint: disable=no-self-use

        reveal_secret = RevealSecret(secret)
        raiden.sign(reveal_secret)

        # first reveal the secret to the last_node in the chain, proceed after
        # ack
        raiden.send_and_wait(last_node, reveal_secret, timeout=None)  # XXX: wait for expiration

        # the last_node has acknowledged the Secret, so we know the exchange
        # has kicked-off, reveal the secret to the exchange_node to
        # avoid interceptions but dont wait
        raiden.send_async(exchange_node, reveal_secret)


class ExchangeTask(BaseMediatedTransferTask):
    """ Counterparty task, responsible to receive a MediatedTransfer for the
    from_transfer and forward a to_transfer with the same hashlock.
    """

    def __init__(self, raiden, from_mediated_transfer, to_token, to_amount, target):
        # pylint: disable=too-many-arguments

        super(ExchangeTask, self).__init__()

        self.raiden = raiden
        self.from_mediated_transfer = from_mediated_transfer
        self.target = target

        self.to_amount = to_amount
        self.to_token = to_token

    def __repr__(self):
        return '<{} {} from_token:{} to_token:{}>'.format(
            self.__class__.__name__,
            pex(self.raiden.address),
            pex(self.from_mediated_transfer.token),
            pex(self.to_token),
        )

    def _run(self):  # pylint: disable=method-hidden,too-many-locals
        fee = 0
        raiden = self.raiden

        from_mediated_transfer = self.from_mediated_transfer
        hashlock = from_mediated_transfer.lock.hashlock

        from_token = from_mediated_transfer.token
        to_token = self.to_token
        to_amount = self.to_amount

        to_graph = raiden.channelgraphs[to_token]
        from_graph = raiden.channelgraphs[from_token]

        from_channel = from_graph.partneraddress_channel[from_mediated_transfer.sender]

        raiden.register_task_for_hashlock(from_token, self, hashlock)
        raiden.register_channel_for_hashlock(from_token, from_channel, hashlock)

        lock_expiration = from_mediated_transfer.lock.expiration - raiden.config['reveal_timeout']
        lock_timeout = lock_expiration - raiden.get_block_number()

        to_routes = to_graph.get_best_routes(
            raiden.addres,
            from_mediated_transfer.initiator,  # route back to the initiator
            from_mediated_transfer.lock.amount,
            lock_timeout,
        )

        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                'EXCHANGE TRANSFER %s -> %s msghash:%s hashlock:%s',
                pex(from_mediated_transfer.target),
                pex(from_mediated_transfer.initiator),
                pex(from_mediated_transfer.hash),
                pex(hashlock),
            )

        secret_request = SecretRequest(
            from_mediated_transfer.identifier,
            from_mediated_transfer.lock.hashlock,
            from_mediated_transfer.lock.amount,
        )
        raiden.sign(secret_request)
        raiden.send_async(from_mediated_transfer.initiator, secret_request)

        for path, to_channel in to_routes:
            to_next_hop = path[1]

            to_mediated_transfer = to_channel.create_mediatedtransfer(
                raiden.address,  # this node is the new initiator
                from_mediated_transfer.initiator,  # the initiator is the target for the to_token
                fee,
                to_amount,
                lock_expiration,
                hashlock,  # use the original hashlock
            )
            raiden.sign(to_mediated_transfer)

            if log.isEnabledFor(logging.DEBUG):
                log.debug(
                    'MEDIATED TRANSFER NEW PATH path:%s hashlock:%s',
                    lpex(path),
                    pex(from_mediated_transfer.lock.hashlock),
                )

            # The interest on the hashlock outlives this task, the secret
            # handling will happen only _once_
            raiden.register_channel_for_hashlock(to_token, to_channel, hashlock)
            to_channel.register_transfer(to_mediated_transfer)

            response = self.send_and_wait_valid(raiden, to_mediated_transfer)

            if log.isEnabledFor(logging.DEBUG):
                log.debug(
                    'EXCHANGE TRANSFER NEW PATH path:%s hashlock:%s',
                    lpex(path),
                    pex(hashlock),
                )

            # only refunds for `from_token` must be considered (check send_and_wait_valid)
            if isinstance(response, RefundTransfer):
                if response.lock.amount != to_mediated_transfer.amount:
                    log.info(
                        'Partner %s sent an invalid refund message with an invalid amount',
                        pex(to_next_hop),
                    )
                    raiden.on_hashlock_result(from_token, hashlock, False)
                    return
                else:
                    to_channel.register_transfer(response)

            elif isinstance(response, Secret):
                # claim the from_token
                raiden.handle_secret(
                    response.identifier,
                    from_token,
                    response.secret,
                    response,
                    hashlock,
                )

                # unlock the to_token
                raiden.handle_secret(
                    response.identifier,
                    to_token,
                    response.secret,
                    response,
                    hashlock,
                )

                self._wait_for_unlock_or_close(
                    raiden,
                    from_graph,
                    from_channel,
                    from_mediated_transfer,
                )

    def send_and_wait_valid(self, raiden, mediated_transfer):
        response_iterator = self._send_and_wait_time(
            raiden,
            mediated_transfer.recipient,
            mediated_transfer,
            raiden.config['msg_timeout'],
        )

        for response in response_iterator:
            if response is None:
                log.error(
                    'EXCHANGE TIMED OUT node:%s hashlock:%s',
                    pex(raiden.address),
                    pex(mediated_transfer.lock.hashlock),
                )
                return None

            if isinstance(response, Secret):
                if sha3(response.secret) != mediated_transfer.lock.hashlock:
                    log.error('Secret doesnt match the hashlock, ignoring.')
                    continue

                return response

            # first check that the message is from a known/valid sender/token
            valid_target = response.target == raiden.address
            valid_sender = response.sender == mediated_transfer.recipient
            valid_token = response.token == mediated_transfer.token

            if not valid_target or not valid_sender or not valid_token:
                log.error(
                    'Invalid message [%s] supplied to the task, ignoring.',
                    repr(response),
                )
                continue

            if isinstance(response, RefundTransfer):
                return response

        return None
