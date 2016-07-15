# -*- coding: utf8 -*-
import pytest

from ethereum import utils
from ethereum import tester
from ethereum.utils import sha3, privtoaddr
from ethereum.tester import TransactionFailed
<<<<<<< HEAD
#from raiden.tests.conftest import discovery_blockchain
from raiden.network.discovery import ContractDiscovery
from raiden.blockchain.abi import get_contract_path

def test_endpointregistry():
	registry_contract_path = get_contract_path('EndpointRegistry.sol')
	state = tester.state()
	assert state.block.number < 1150000
	state.block.number = 1158001
	assert state.block.number > 1150000
	sender = tester.a0
	registry_contract = state.abi_contract(None, path=registry_contract_path, language="solidity")
	sender = tester.a0.encode('hex')
	registry_contract.registerEndpoint('127.0.0.1:4001')
	assert registry_contract.findAddressByEndpoint('127.0.0.1:4001') == tester.a0.encode('hex')
	assert registry_contract.findEndpointByAddress(tester.a0.encode('hex')) == '127.0.0.1:4001'
	registry_contract.updateEndpoint('192.168.0.1:4002')
	assert registry_contract.findAddressByEndpoint('192.168.0.1:4002') == tester.a0.encode('hex')
	assert registry_contract.findEndpointByAddress(tester.a0.encode('hex')) == '192.168.0.1:4002'

