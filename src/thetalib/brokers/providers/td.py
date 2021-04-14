import sys
import os
import http.server
import ssl
import urllib.parse
import threading
import webbrowser
import json
from decimal import Decimal
import re

import requests
import dateutil.parser

from thetalib.brokers.base import (
    AssetType,
    Broker,
    PositionEffect,
    Instruction,
    OptionType,
    Trade,
)
from thetalib.brokers.providers.selfsigned import generate_selfsigned_cert
from thetalib.config import get_user_data_dir


REDIRECT_URL = "https://127.0.0.1:42068/callback"


class TdAuth():
    def __init__(self, consumer_key):
        self._consumer_key = consumer_key
        self._returned_code = None
        super().__init__()

    def get_access_token(self):
        server = threading.Thread(target=self._start_server)
        server.setDaemon(True)
        server.start()

        redirect_uri = urllib.parse.quote(REDIRECT_URL)
        consumer_key = urllib.parse.quote(self._consumer_key)
        url = ("https://auth.tdameritrade.com/auth?"
               "response_type=code&"
               f"redirect_uri={redirect_uri}&"
               f"client_id={consumer_key}%40AMER.OAUTHAP"
               "&scope=AccountAccess")
        print(f"Opening {url}")
        print("Please authorize the app from your web browser.")
        print("You may see a warning about using a self-signed certificate.")
        webbrowser.open(url)
        server.join()
        data = {
            'grant_type': 'authorization_code',
            'refresh_token': None,
            'access_type': 'offline',
            'code': self._returned_code,
            'client_id': self._consumer_key,
            'redirect_uri': REDIRECT_URL,
        }
        token_url = "https://api.tdameritrade.com/v1/oauth2/token"
        rsp = requests.post(token_url, data=data)
        return rsp.json()['access_token']

    def _start_server(self):
        # LOOK AWAY!!! D:

        class MyRequestHandler(http.server.BaseHTTPRequestHandler):
            returned_code = None

            def do_GET(self):
                parsed_url = urllib.parse.urlparse(self.path)
                qs = urllib.parse.parse_qs(parsed_url.query)
                MyRequestHandler.keep_running = False
                MyRequestHandler.returned_code = qs['code'][0]
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(("<html><body>Yay! You may now close "
                                  "this window."
                                  "</body></html>").encode("utf-8"))

        data_dir = get_user_data_dir()
        pemfile = os.path.join(data_dir, 'cert.pem')
        if not os.path.isfile(pemfile):
            cert_pem, key_pem = generate_selfsigned_cert('localhost')
            with open(pemfile, 'w+') as f:
                f.write(cert_pem.decode("utf-8"))
                f.write(key_pem.decode("utf-8"))

        address = ('', 42068)
        httpd = http.server.HTTPServer(address, MyRequestHandler)
        httpd.socket = ssl.wrap_socket(
            httpd.socket,
            server_side=True,
            certfile=pemfile,
            ssl_version=ssl.PROTOCOL_TLS,
        )
        while MyRequestHandler.returned_code is None:
            httpd.handle_request()
        self._returned_code = MyRequestHandler.returned_code


def get_access_token(consumer_key):
    return TdAuth(consumer_key).get_access_token()


class TdAPI:
    API_BASE = 'https://api.tdameritrade.com'

    def __init__(self, access_token):
        self._access_token = access_token

    def _request(self, method, path):
        headers = {"Authorization": f"Bearer {self._access_token}"}
        fn = {
            'get': requests.get,
        }[method]
        if path[0] != '/':
            path = '/' + path
        url = TdAPI.API_BASE + path
        return fn(url, headers=headers)

    def get(self, path):
        return self._request('get', path)


def option_symbol_parse_strike(option_symbol):
    """
    Given "CHPT_041621C30", returns Decimal('30')
    """
    match = re.search(r'[PC]{1}([0-9]+)', option_symbol)
    if match:
        return Decimal(match.group(1))


class TdTrade(Trade):
    """
    Trade class for TD.
    """

    def __init__(self, api_object):
        if api_object['type'] != 'TRADE':
            raise ValueError("TdTrade only understands TRADE objects")

        self.api_object = api_object
        asset_type = self._get_asset_type()
        instrument = api_object['transactionItem']['instrument']
        if asset_type == AssetType.EQUITY:
            symbol = instrument['symbol']
            option_expiration = None
            strike = None
            option_symbol = None
        else:
            symbol = instrument['underlyingSymbol']
            option_expiration = dateutil.parser.parse(
                instrument['optionExpirationDate'])
            strike = option_symbol_parse_strike(instrument['symbol'])
            option_symbol = instrument['symbol']

        super().__init__(
            api_object,
            dateutil.parser.parse(api_object['transactionDate']),
            dateutil.parser.parse(api_object['orderDate']),
            dateutil.parser.parse(api_object['settlementDate']).date(),
            self._get_instruction(),
            asset_type,
            self._get_option_type(asset_type),
            self._get_position_effect(asset_type),
            self._get_fees_and_commission(),
            Decimal(api_object['transactionItem']['amount']),
            Decimal(str(api_object['transactionItem']['price'])),
            symbol,
            option_expiration,
            strike,
            option_symbol,
        )

    def _get_instruction(self):
        instruction = self.api_object['transactionItem']['instruction']
        if instruction == 'BUY':
            return Instruction.BUY
        return Instruction.SELL

    def _get_asset_type(self):
        atype = self.api_object['transactionItem']['instrument']['assetType']
        if atype == 'OPTION':
            return AssetType.OPTION
        return AssetType.EQUITY

    def _get_option_type(self, asset_type):
        if asset_type == AssetType.EQUITY:
            return None
        otype = self.api_object['transactionItem']['instrument']['putCall']
        if otype == 'CALL':
            return OptionType.CALL
        return OptionType.PUT

    def _get_position_effect(self, asset_type):
        if asset_type == AssetType.EQUITY:
            return None
        peffect = self.api_object['transactionItem']['positionEffect']
        if peffect == 'OPENING':
            return PositionEffect.OPEN
        return PositionEffect.CLOSE

    def _get_fees_and_commission(self):
        return sum(Decimal(str(f)) for f in self.api_object['fees'].values())


class BrokerTd(Broker):
    """
    Broker class for TD.
    """

    provider_name = "td"

    def __init__(self, access_token, account_id, account_name, test_file=None):
        super().__init__()
        self.access_token = access_token
        self.account_name = account_name
        self.account_id = account_id
        self._api = TdAPI(access_token)
        self._account_info = None
        self._trades = None
        self._test_data = None
        self._test_file = test_file
        if test_file is not None:
            with open(os.path.expanduser(test_file)) as f:
                self._test_data = json.loads(f.read())

    def _get_transactions(self):
        if self._test_data:
            return self._test_data
        url = f'/v1/accounts/{self.account_id}/transactions'
        return self._api.get(url).json()

    def get_trades(self):
        if self._trades is None:
            self._trades = [
                TdTrade(t)
                for t in self._get_transactions() if t['type'] == 'TRADE'
            ]
        return self._trades

    @classmethod
    def from_config(cls, config):
        cdata = config["data"]
        if "file" in cdata:
            return cls(None, test_file=cdata["file"])
        return cls(cdata["access_token"], cdata["account_id"], config["name"])

    def to_config(self):
        if self._test_file is not None:
            config = {"file": self._test_file}
        else:
            config = {
                "access_token": self.access_token,
                "account_id": self.account_id,
            }
        return config

    @classmethod
    def UI_add(cls, account_name):
        print()
        print("Initializing TD")
        print("Please make a selection:")
        print("  (1) Configure automatically")
        print("  (2) Enter token/test file manually")
        selection = None
        while selection not in ("1", "2"):
            selection = input(" >> ")
        if selection == "1":
            print()
            print("Please follow the TD API Getting Started guide [1] to")
            print("create a developer account and an app with a callback")
            print(f"URL of {REDIRECT_URL}")
            print("Then paste your app's consumer key here:")
            print("[1] https://developer.tdameritrade.com/content/getting-started")
            print()
            consumer_key = input(" >> ")
            access_token = get_access_token(consumer_key)
        else:
            print("Please enter your TD API access token:")
            access_token = input(" >> ")
        if os.path.isfile(os.path.expanduser(access_token)):
            config = {"file": access_token}
        else:
            accounts = TdAPI(access_token).get('/v1/accounts').json()
            print("Please select an account:")
            for (i, account) in enumerate(accounts):
                print(f"  ({i+1}) {account['securitiesAccount']['accountId']}")
            while True:
                try:
                    acc_idx = int(input(" >> "))
                    account = accounts[acc_idx - 1]
                    break
                except Exception:
                    print("Invalid selection")

            config = {
                "access_token": access_token,
                "account_id": account["securitiesAccount"]["accountId"]
            }
        return cls.from_config({"data": config, "name": account_name})


def _main():
    access_token = sys.argv[1]
    broker = BrokerTd(access_token, test_file=sys.argv[2])
    print("Trades:")
    print('\n'.join(str(t) for t in broker.get_trades()))


if __name__ == "__main__":
    _main()
