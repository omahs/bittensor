# The MIT License (MIT)
# Copyright © 2021 Yuma Rao

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import argparse
import os
import sys
import re
from typing import List, Union, Optional, Dict, Tuple

from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.console import Console
from rich.text import Text
from tqdm import tqdm

import bittensor
from bittensor.utils.balance import Balance
from .utils import (
    get_hotkey_wallets_for_wallet,
    get_delegates_details,
    DelegatesDetails,
)
from . import defaults  # type: ignore
from .. import ChildInfo
from ..utils import wallet_utils
from ..utils.formatting import u64_to_float

console = bittensor.__console__


class StakeCommand:
    """
    Executes the ``add`` command to stake tokens to one or more hotkeys from a user's coldkey on the Bittensor network.

    This command is used to allocate tokens to different hotkeys, securing their position and influence on the network.

    Usage:
        Users can specify the amount to stake, the hotkeys to stake to (either by name or ``SS58`` address), and whether to stake to all hotkeys. The command checks for sufficient balance and hotkey registration
        before proceeding with the staking process.

    Optional arguments:
        - ``--all`` (bool): When set, stakes all available tokens from the coldkey.
        - ``--uid`` (int): The unique identifier of the neuron to which the stake is to be added.
        - ``--amount`` (float): The amount of TAO tokens to stake.
        - ``--max_stake`` (float): Sets the maximum amount of TAO to have staked in each hotkey.
        - ``--hotkeys`` (list): Specifies hotkeys by name or SS58 address to stake to.
        - ``--all_hotkeys`` (bool): When set, stakes to all hotkeys associated with the wallet, excluding any specified in --hotkeys.

    The command prompts for confirmation before executing the staking operation.

    Example usage::

        btcli stake add --amount 100 --wallet.name <my_wallet> --wallet.hotkey <my_hotkey>

    Note:
        This command is critical for users who wish to distribute their stakes among different neurons (hotkeys) on the network.
        It allows for a strategic allocation of tokens to enhance network participation and influence.
    """

    @staticmethod
    def run(cli: "bittensor.cli"):
        r"""Stake token of amount to hotkey(s)."""
        try:
            config = cli.config.copy()
            subtensor: "bittensor.subtensor" = bittensor.subtensor(
                config=config, log_verbose=False
            )
            StakeCommand._run(cli, subtensor)
        finally:
            if "subtensor" in locals():
                subtensor.close()
                bittensor.logging.debug("closing subtensor connection")

    @staticmethod
    def _run(cli: "bittensor.cli", subtensor: "bittensor.subtensor"):
        r"""Stake token of amount to hotkey(s)."""
        config = cli.config.copy()
        wallet = bittensor.wallet(config=config)

        # Get the hotkey_names (if any) and the hotkey_ss58s.
        hotkeys_to_stake_to: List[Tuple[Optional[str], str]] = []
        if config.get("all_hotkeys"):
            # Stake to all hotkeys.
            all_hotkeys: List[bittensor.wallet] = get_hotkey_wallets_for_wallet(
                wallet=wallet
            )
            # Get the hotkeys to exclude. (d)efault to no exclusions.
            hotkeys_to_exclude: List[str] = cli.config.get("hotkeys", d=[])
            # Exclude hotkeys that are specified.
            hotkeys_to_stake_to = [
                (wallet.hotkey_str, wallet.hotkey.ss58_address)
                for wallet in all_hotkeys
                if wallet.hotkey_str not in hotkeys_to_exclude
            ]  # definitely wallets

        elif config.get("hotkeys"):
            # Stake to specific hotkeys.
            for hotkey_ss58_or_hotkey_name in config.get("hotkeys"):
                if bittensor.utils.is_valid_ss58_address(hotkey_ss58_or_hotkey_name):
                    # If the hotkey is a valid ss58 address, we add it to the list.
                    hotkeys_to_stake_to.append((None, hotkey_ss58_or_hotkey_name))
                else:
                    # If the hotkey is not a valid ss58 address, we assume it is a hotkey name.
                    #  We then get the hotkey from the wallet and add it to the list.
                    wallet_ = bittensor.wallet(
                        config=config, hotkey=hotkey_ss58_or_hotkey_name
                    )
                    hotkeys_to_stake_to.append(
                        (wallet_.hotkey_str, wallet_.hotkey.ss58_address)
                    )
        elif config.wallet.get("hotkey"):
            # Only config.wallet.hotkey is specified.
            #  so we stake to that single hotkey.
            hotkey_ss58_or_name = config.wallet.get("hotkey")
            if bittensor.utils.is_valid_ss58_address(hotkey_ss58_or_name):
                hotkeys_to_stake_to = [(None, hotkey_ss58_or_name)]
            else:
                # Hotkey is not a valid ss58 address, so we assume it is a hotkey name.
                wallet_ = bittensor.wallet(config=config, hotkey=hotkey_ss58_or_name)
                hotkeys_to_stake_to = [
                    (wallet_.hotkey_str, wallet_.hotkey.ss58_address)
                ]
        else:
            # Only config.wallet.hotkey is specified.
            #  so we stake to that single hotkey.
            assert config.wallet.hotkey is not None
            hotkeys_to_stake_to = [
                (None, bittensor.wallet(config=config).hotkey.ss58_address)
            ]

        # Get coldkey balance
        wallet_balance: Balance = subtensor.get_balance(wallet.coldkeypub.ss58_address)
        final_hotkeys: List[Tuple[str, str]] = []
        final_amounts: List[Union[float, Balance]] = []
        for hotkey in tqdm(hotkeys_to_stake_to):
            hotkey: Tuple[Optional[str], str]  # (hotkey_name (or None), hotkey_ss58)
            if not subtensor.is_hotkey_registered_any(hotkey_ss58=hotkey[1]):
                # Hotkey is not registered.
                if len(hotkeys_to_stake_to) == 1:
                    # Only one hotkey, error
                    bittensor.__console__.print(
                        f"[red]Hotkey [bold]{hotkey[1]}[/bold] is not registered. Aborting.[/red]"
                    )
                    return None
                else:
                    # Otherwise, print warning and skip
                    bittensor.__console__.print(
                        f"[yellow]Hotkey [bold]{hotkey[1]}[/bold] is not registered. Skipping.[/yellow]"
                    )
                    continue

            stake_amount_tao: float = config.get("amount")
            if config.get("max_stake"):
                # Get the current stake of the hotkey from this coldkey.
                hotkey_stake: Balance = subtensor.get_stake_for_coldkey_and_hotkey(
                    hotkey_ss58=hotkey[1], coldkey_ss58=wallet.coldkeypub.ss58_address
                )
                stake_amount_tao: float = config.get("max_stake") - hotkey_stake.tao

                # If the max_stake is greater than the current wallet balance, stake the entire balance.
                stake_amount_tao: float = min(stake_amount_tao, wallet_balance.tao)
                if (
                    stake_amount_tao <= 0.00001
                ):  # Threshold because of fees, might create a loop otherwise
                    # Skip hotkey if max_stake is less than current stake.
                    continue
                wallet_balance = Balance.from_tao(wallet_balance.tao - stake_amount_tao)

                if wallet_balance.tao < 0:
                    # No more balance to stake.
                    break

            final_amounts.append(stake_amount_tao)
            final_hotkeys.append(hotkey)  # add both the name and the ss58 address.

        if len(final_hotkeys) == 0:
            # No hotkeys to stake to.
            bittensor.__console__.print(
                "Not enough balance to stake to any hotkeys or max_stake is less than current stake."
            )
            return None

        # Ask to stake
        if not config.no_prompt:
            if not Confirm.ask(
                f"Do you want to stake to the following keys from {wallet.name}:\n"
                + "".join(
                    [
                        f"    [bold white]- {hotkey[0] + ':' if hotkey[0] else ''}{hotkey[1]}: {f'{amount} {bittensor.__tao_symbol__}' if amount else 'All'}[/bold white]\n"
                        for hotkey, amount in zip(final_hotkeys, final_amounts)
                    ]
                )
            ):
                return None

        if len(final_hotkeys) == 1:
            # do regular stake
            return subtensor.add_stake(
                wallet=wallet,
                hotkey_ss58=final_hotkeys[0][1],
                amount=None if config.get("stake_all") else final_amounts[0],
                wait_for_inclusion=True,
                prompt=not config.no_prompt,
            )

        subtensor.add_stake_multiple(
            wallet=wallet,
            hotkey_ss58s=[hotkey_ss58 for _, hotkey_ss58 in final_hotkeys],
            amounts=None if config.get("stake_all") else final_amounts,
            wait_for_inclusion=True,
            prompt=False,
        )

    @classmethod
    def check_config(cls, config: "bittensor.config"):
        if not config.is_set("wallet.name") and not config.no_prompt:
            wallet_name = Prompt.ask("Enter wallet name", default=defaults.wallet.name)
            config.wallet.name = str(wallet_name)

        if (
            not config.is_set("wallet.hotkey")
            and not config.no_prompt
            and not config.wallet.get("all_hotkeys")
            and not config.wallet.get("hotkeys")
        ):
            hotkey = Prompt.ask("Enter hotkey name", default=defaults.wallet.hotkey)
            config.wallet.hotkey = str(hotkey)

        # Get amount.
        if (
            not config.get("amount")
            and not config.get("stake_all")
            and not config.get("max_stake")
        ):
            if not Confirm.ask(
                "Stake all Tao from account: [bold]'{}'[/bold]?".format(
                    config.wallet.get("name", defaults.wallet.name)
                )
            ):
                amount = Prompt.ask("Enter Tao amount to stake")
                try:
                    config.amount = float(amount)
                except ValueError:
                    console.print(
                        ":cross_mark:[red]Invalid Tao amount[/red] [bold white]{}[/bold white]".format(
                            amount
                        )
                    )
                    sys.exit()
            else:
                config.stake_all = True

    @classmethod
    def add_args(cls, parser: argparse.ArgumentParser):
        stake_parser = parser.add_parser(
            "add", help="""Add stake to your hotkey accounts from your coldkey."""
        )
        stake_parser.add_argument("--all", dest="stake_all", action="store_true")
        stake_parser.add_argument("--uid", dest="uid", type=int, required=False)
        stake_parser.add_argument("--amount", dest="amount", type=float, required=False)
        stake_parser.add_argument(
            "--max_stake",
            dest="max_stake",
            type=float,
            required=False,
            action="store",
            default=None,
            help="""Specify the maximum amount of Tao to have staked in each hotkey.""",
        )
        stake_parser.add_argument(
            "--hotkeys",
            "--exclude_hotkeys",
            "--wallet.hotkeys",
            "--wallet.exclude_hotkeys",
            required=False,
            action="store",
            default=[],
            type=str,
            nargs="*",
            help="""Specify the hotkeys by name or ss58 address. (e.g. hk1 hk2 hk3)""",
        )
        stake_parser.add_argument(
            "--all_hotkeys",
            "--wallet.all_hotkeys",
            required=False,
            action="store_true",
            default=False,
            help="""To specify all hotkeys. Specifying hotkeys will exclude them from this all.""",
        )
        bittensor.wallet.add_args(stake_parser)
        bittensor.subtensor.add_args(stake_parser)


def _get_coldkey_wallets_for_path(path: str) -> List["bittensor.wallet"]:
    try:
        wallet_names = next(os.walk(os.path.expanduser(path)))[1]
        return [bittensor.wallet(path=path, name=name) for name in wallet_names]
    except StopIteration:
        # No wallet files found.
        wallets = []
    return wallets


def _get_hotkey_wallets_for_wallet(wallet) -> List["bittensor.wallet"]:
    hotkey_wallets = []
    hotkeys_path = wallet.path + "/" + wallet.name + "/hotkeys"
    try:
        hotkey_files = next(os.walk(os.path.expanduser(hotkeys_path)))[2]
    except StopIteration:
        hotkey_files = []
    for hotkey_file_name in hotkey_files:
        try:
            hotkey_for_name = bittensor.wallet(
                path=wallet.path, name=wallet.name, hotkey=hotkey_file_name
            )
            if (
                hotkey_for_name.hotkey_file.exists_on_device()
                and not hotkey_for_name.hotkey_file.is_encrypted()
            ):
                hotkey_wallets.append(hotkey_for_name)
        except Exception:
            pass
    return hotkey_wallets


class StakeShow:
    """
    Executes the ``show`` command to list all stake accounts associated with a user's wallet on the Bittensor network.

    This command provides a comprehensive view of the stakes associated with both hotkeys and delegates linked to the user's coldkey.

    Usage:
        The command lists all stake accounts for a specified wallet or all wallets in the user's configuration directory.
        It displays the coldkey, balance, account details (hotkey/delegate name), stake amount, and the rate of return.

    Optional arguments:
        - ``--all`` (bool): When set, the command checks all coldkey wallets instead of just the specified wallet.

    The command compiles a table showing:

    - Coldkey: The coldkey associated with the wallet.
    - Balance: The balance of the coldkey.
    - Account: The name of the hotkey or delegate.
    - Stake: The amount of TAO staked to the hotkey or delegate.
    - Rate: The rate of return on the stake, typically shown in TAO per day.

    Example usage::

        btcli stake show --all

    Note:
        This command is essential for users who wish to monitor their stake distribution and returns across various accounts on the Bittensor network.
        It provides a clear and detailed overview of the user's staking activities.
    """

    @staticmethod
    def run(cli: "bittensor.cli"):
        r"""Show all stake accounts."""
        try:
            subtensor: "bittensor.subtensor" = bittensor.subtensor(
                config=cli.config, log_verbose=False
            )
            StakeShow._run(cli, subtensor)
        finally:
            if "subtensor" in locals():
                subtensor.close()
                bittensor.logging.debug("closing subtensor connection")

    @staticmethod
    def _run(cli: "bittensor.cli", subtensor: "bittensor.subtensor"):
        r"""Show all stake accounts."""
        if cli.config.get("all", d=False) == True:
            wallets = _get_coldkey_wallets_for_path(cli.config.wallet.path)
        else:
            wallets = [bittensor.wallet(config=cli.config)]
        registered_delegate_info: Optional[Dict[str, DelegatesDetails]] = (
            get_delegates_details(url=bittensor.__delegates_details_url__)
        )

        def get_stake_accounts(
            wallet, subtensor
        ) -> Dict[str, Dict[str, Union[str, Balance]]]:
            """Get stake account details for the given wallet.

            Args:
                wallet: The wallet object to fetch the stake account details for.

            Returns:
                A dictionary mapping SS58 addresses to their respective stake account details.
            """

            wallet_stake_accounts = {}

            # Get this wallet's coldkey balance.
            cold_balance = subtensor.get_balance(wallet.coldkeypub.ss58_address)

            # Populate the stake accounts with local hotkeys data.
            wallet_stake_accounts.update(get_stakes_from_hotkeys(subtensor, wallet))

            # Populate the stake accounts with delegations data.
            wallet_stake_accounts.update(get_stakes_from_delegates(subtensor, wallet))

            return {
                "name": wallet.name,
                "balance": cold_balance,
                "accounts": wallet_stake_accounts,
            }

        def get_stakes_from_hotkeys(
            subtensor, wallet
        ) -> Dict[str, Dict[str, Union[str, Balance]]]:
            """Fetch stakes from hotkeys for the provided wallet.

            Args:
                wallet: The wallet object to fetch the stakes for.

            Returns:
                A dictionary of stakes related to hotkeys.
            """
            hotkeys = get_hotkey_wallets_for_wallet(wallet)
            stakes = {}
            for hot in hotkeys:
                emission = sum(
                    [
                        n.emission
                        for n in subtensor.get_all_neurons_for_pubkey(
                            hot.hotkey.ss58_address
                        )
                    ]
                )
                hotkey_stake = subtensor.get_stake_for_coldkey_and_hotkey(
                    hotkey_ss58=hot.hotkey.ss58_address,
                    coldkey_ss58=wallet.coldkeypub.ss58_address,
                )
                stakes[hot.hotkey.ss58_address] = {
                    "name": hot.hotkey_str,
                    "stake": hotkey_stake,
                    "rate": emission,
                }
            return stakes

        def get_stakes_from_delegates(
            subtensor, wallet
        ) -> Dict[str, Dict[str, Union[str, Balance]]]:
            """Fetch stakes from delegates for the provided wallet.

            Args:
                wallet: The wallet object to fetch the stakes for.

            Returns:
                A dictionary of stakes related to delegates.
            """
            delegates = subtensor.get_delegated(
                coldkey_ss58=wallet.coldkeypub.ss58_address
            )
            stakes = {}
            for dele, staked in delegates:
                for nom in dele.nominators:
                    if nom[0] == wallet.coldkeypub.ss58_address:
                        delegate_name = (
                            registered_delegate_info[dele.hotkey_ss58].name
                            if dele.hotkey_ss58 in registered_delegate_info
                            else dele.hotkey_ss58
                        )
                        stakes[dele.hotkey_ss58] = {
                            "name": delegate_name,
                            "stake": nom[1],
                            "rate": dele.total_daily_return.tao
                            * (nom[1] / dele.total_stake.tao),
                        }
            return stakes

        def get_all_wallet_accounts(
            wallets,
            subtensor,
        ) -> List[Dict[str, Dict[str, Union[str, Balance]]]]:
            """Fetch stake accounts for all provided wallets using a ThreadPool.

            Args:
                wallets: List of wallets to fetch the stake accounts for.

            Returns:
                A list of dictionaries, each dictionary containing stake account details for each wallet.
            """

            accounts = []
            # Create a progress bar using tqdm
            with tqdm(total=len(wallets), desc="Fetching accounts", ncols=100) as pbar:
                for wallet in wallets:
                    accounts.append(get_stake_accounts(wallet, subtensor))
                    pbar.update()
            return accounts

        accounts = get_all_wallet_accounts(wallets, subtensor)

        total_stake = 0
        total_balance = 0
        total_rate = 0
        for acc in accounts:
            total_balance += acc["balance"].tao
            for key, value in acc["accounts"].items():
                total_stake += value["stake"].tao
                total_rate += float(value["rate"])
        table = Table(show_footer=True, pad_edge=False, box=None, expand=False)
        table.add_column(
            "[overline white]Coldkey", footer_style="overline white", style="bold white"
        )
        table.add_column(
            "[overline white]Balance",
            "\u03c4{:.5f}".format(total_balance),
            footer_style="overline white",
            style="green",
        )
        table.add_column(
            "[overline white]Account", footer_style="overline white", style="blue"
        )
        table.add_column(
            "[overline white]Stake",
            "\u03c4{:.5f}".format(total_stake),
            footer_style="overline white",
            style="green",
        )
        table.add_column(
            "[overline white]Rate",
            "\u03c4{:.5f}/d".format(total_rate),
            footer_style="overline white",
            style="green",
        )
        for acc in accounts:
            table.add_row(acc["name"], acc["balance"], "", "")
            for key, value in acc["accounts"].items():
                table.add_row(
                    "", "", value["name"], value["stake"], str(value["rate"]) + "/d"
                )
        bittensor.__console__.print(table)

    @staticmethod
    def check_config(config: "bittensor.config"):
        if (
            not config.get("all", d=None)
            and not config.is_set("wallet.name")
            and not config.no_prompt
        ):
            wallet_name = Prompt.ask("Enter wallet name", default=defaults.wallet.name)
            config.wallet.name = str(wallet_name)

    @staticmethod
    def add_args(parser: argparse.ArgumentParser):
        list_parser = parser.add_parser(
            "show", help="""List all stake accounts for wallet."""
        )
        list_parser.add_argument(
            "--all",
            action="store_true",
            help="""Check all coldkey wallets.""",
            default=False,
        )

        bittensor.wallet.add_args(list_parser)
        bittensor.subtensor.add_args(list_parser)


class SetChildCommand:
    """
    Executes the ``set_child`` command to add a child hotkey on a specified subnet on the Bittensor network.

    This command is used to delegate authority to different hotkeys, securing their position and influence on the subnet.

    Usage:
        Users can specify the amount or 'proportion' to delegate to a child hotkey (either by name or ``SS58`` address),
        the user needs to have sufficient authority to make this call, and the sum of proportions cannot be greater than 1.

    The command prompts for confirmation before executing the set_child operation.

    Example usage::

        btcli stake set_child --child <child_hotkey> --hotkey <parent_hotkey> --netuid 1 --proportion 0.5

    Note:
        This command is critical for users who wish to delegate child hotkeys among different neurons (hotkeys) on the network.
        It allows for a strategic allocation of authority to enhance network participation and influence.
    """

    @staticmethod
    def run(cli: "bittensor.cli"):
        """Set child hotkey."""
        try:
            subtensor: "bittensor.subtensor" = bittensor.subtensor(
                config=cli.config, log_verbose=False
            )
            SetChildCommand._run(cli, subtensor)
        finally:
            if "subtensor" in locals():
                subtensor.close()
                bittensor.logging.debug("closing subtensor connection")

    @staticmethod
    def _run(cli: "bittensor.cli", subtensor: "bittensor.subtensor"):
        wallet = bittensor.wallet(config=cli.config)

        GetChildrenCommand.run(cli)

        # Get values if not set.
        if not cli.config.is_set("netuid"):
            cli.config.netuid = int(Prompt.ask("Enter netuid"))

        if not cli.config.is_set("child"):
            cli.config.child = Prompt.ask("Enter child hotkey (ss58)")

        if not cli.config.is_set("hotkey"):
            cli.config.hotkey = Prompt.ask("Enter parent hotkey (ss58)")

        if not cli.config.is_set("proportion"):
            cli.config.proportion = Prompt.ask("Enter proportion")

        # Parse from strings
        netuid = cli.config.netuid

        try:
            proportion = float(cli.config.proportion)
        except ValueError:
            console.print(
                ":cross_mark:[red] Invalid proportion amount[/red] [bold white]{}[/bold white]".format(
                    cli.config.proportion
                )
            )
            sys.exit()

        if proportion > 1:
            raise ValueError(
                f":cross_mark:[red] The sum of all proportions cannot be greater than 1. Proposed proportion is {proportion}[/red]"
            )

        if not wallet_utils.is_valid_ss58_address(cli.config.child):
            raise ValueError(
                f":cross_mark:[red] Child ss58 address: {cli.config.child} unrecognizable. Please check child address and try again.[/red]"
            )

        success, message = subtensor.set_child_singular(
            wallet=wallet,
            netuid=netuid,
            child=cli.config.child,
            hotkey=cli.config.hotkey,
            proportion=proportion,
            wait_for_inclusion=cli.config.wait_for_inclusion,
            wait_for_finalization=cli.config.wait_for_finalization,
            prompt=cli.config.prompt,
        )

        # Result
        if success:
            console.print(":white_heavy_check_mark: [green]Set child hotkey.[/green]")
        else:
            console.print(
                f":cross_mark:[red] Unable to set child hotkey.[/red] {message}"
            )

    @staticmethod
    def check_config(config: "bittensor.config"):
        if not config.is_set("wallet.name") and not config.no_prompt:
            wallet_name = Prompt.ask("Enter wallet name", default=defaults.wallet.name)
            config.wallet.name = str(wallet_name)
        if not config.is_set("wallet.hotkey") and not config.no_prompt:
            hotkey = Prompt.ask("Enter hotkey name", default=defaults.wallet.hotkey)
            config.wallet.hotkey = str(hotkey)

    @staticmethod
    def add_args(parser: argparse.ArgumentParser):
        parser = parser.add_parser("set_child", help="""Set a child hotkey.""")
        parser.add_argument("--netuid", dest="netuid", type=int, required=False)
        parser.add_argument("--child", dest="child", type=str, required=False)
        parser.add_argument("--hotkey", dest="hotkey", type=str, required=False)
        parser.add_argument("--proportion", dest="proportion", type=str, required=False)
        parser.add_argument(
            "--wait-for-inclusion",
            dest="wait_for_inclusion",
            action="store_true",
            default=False,
        )
        parser.add_argument(
            "--wait-for-finalization",
            dest="wait_for_finalization",
            action="store_true",
            default=True,
        )
        parser.add_argument(
            "--prompt",
            dest="prompt",
            action="store_true",
            default=False,
        )
        bittensor.wallet.add_args(parser)
        bittensor.subtensor.add_args(parser)


class SetChildrenCommand:
    """
    Executes the ``set_children`` command to add children hotkeys on a specified subnet on the Bittensor network.

    This command is used to delegate authority to different hotkeys, securing their position and influence on the subnet.

    Usage:
        Users can specify the amount or 'proportion' to delegate to a child hotkey (either by name or ``SS58`` address),
        the user needs to have sufficient authority to make this call, and the sum of proportions cannot be greater than 1.

    The command prompts for confirmation before executing the set_children operation.

    Example usage::

        btcli stake set_children --children <child_hotkey>,<child_hotkey> --hotkey <parent_hotkey> --netuid 1 --proportion 0.3,0.3

    Note:
        This command is critical for users who wish to delegate children hotkeys among different neurons (hotkeys) on the network.
        It allows for a strategic allocation of authority to enhance network participation and influence.
    """

    @staticmethod
    def run(cli: "bittensor.cli"):
        """Set children hotkeys."""
        try:
            subtensor: "bittensor.subtensor" = bittensor.subtensor(
                config=cli.config, log_verbose=False
            )
            SetChildrenCommand._run(cli, subtensor)
        finally:
            if "subtensor" in locals():
                subtensor.close()
                bittensor.logging.debug("closing subtensor connection")

    @staticmethod
    def _run(cli: "bittensor.cli", subtensor: "bittensor.subtensor"):
        wallet = bittensor.wallet(config=cli.config)

        GetChildrenCommand.run(cli)

        # Get values if not set.
        if not cli.config.is_set("netuid"):
            cli.config.netuid = int(Prompt.ask("Enter netuid"))

        if not cli.config.is_set("children"):
            cli.config.children = Prompt.ask(
                "Enter children hotkey (ss58) as comma-separated values"
            )

        if not cli.config.is_set("hotkey"):
            cli.config.hotkey = Prompt.ask("Enter parent hotkey (ss58)")

        if not cli.config.is_set("proportions"):
            cli.config.proportions = Prompt.ask(
                "Enter proportions for children as comma-separated values (sum less than 1)"
            )

        # Parse from strings
        netuid = cli.config.netuid

        # extract proportions and child addresses from cli input
        proportions = [float(x) for x in re.split(r"[ ,]+", cli.config.proportions)]
        children = [str(x) for x in re.split(r"[ ,]+", cli.config.children)]

        # Validate children SS58 addresses
        for child in children:
            if not wallet_utils.is_valid_ss58_address(child):
                console.print(f":cross_mark:[red] Invalid SS58 address: {child}[/red]")
                return

        total_proposed = sum(proportions)
        if total_proposed > 1:
            raise ValueError(
                f"The sum of all proportions cannot be greater than 1. Proposed sum of proportions is {total_proposed}."
            )

        success, message = subtensor.set_children_multiple(
            wallet=wallet,
            netuid=netuid,
            children=children,
            hotkey=cli.config.hotkey,
            proportions=proportions,
            wait_for_inclusion=cli.config.wait_for_inclusion,
            wait_for_finalization=cli.config.wait_for_finalization,
            prompt=cli.config.prompt,
        )

        # Result
        if success:
            console.print(
                ":white_heavy_check_mark: [green]Set children hotkeys.[/green]"
            )
        else:
            console.print(
                f":cross_mark:[red] Unable to set children hotkeys.[/red] {message}"
            )

    @staticmethod
    def check_config(config: "bittensor.config"):
        if not config.is_set("wallet.name") and not config.no_prompt:
            wallet_name = Prompt.ask("Enter wallet name", default=defaults.wallet.name)
            config.wallet.name = str(wallet_name)
        if not config.is_set("wallet.hotkey") and not config.no_prompt:
            hotkey = Prompt.ask("Enter hotkey name", default=defaults.wallet.hotkey)
            config.wallet.hotkey = str(hotkey)

    @staticmethod
    def add_args(parser: argparse.ArgumentParser):
        set_children_parser = parser.add_parser(
            "set_children", help="""Set multiple children hotkeys."""
        )
        set_children_parser.add_argument(
            "--netuid", dest="netuid", type=int, required=False
        )
        set_children_parser.add_argument(
            "--children", dest="children", type=str, required=False
        )
        set_children_parser.add_argument(
            "--hotkey", dest="hotkey", type=str, required=False
        )
        set_children_parser.add_argument(
            "--proportions", dest="proportions", type=str, required=False
        )
        set_children_parser.add_argument(
            "--wait-for-inclusion",
            dest="wait_for_inclusion",
            action="store_true",
            default=False,
        )
        set_children_parser.add_argument(
            "--wait-for-finalization",
            dest="wait_for_finalization",
            action="store_true",
            default=True,
        )
        set_children_parser.add_argument(
            "--prompt",
            dest="prompt",
            action="store_true",
            default=False,
        )
        bittensor.wallet.add_args(set_children_parser)
        bittensor.subtensor.add_args(set_children_parser)


class GetChildrenCommand:
    """
    Executes the ``get_children_info`` command to get all child hotkeys on a specified subnet on the Bittensor network.

    This command is used to view delegated authority to different hotkeys on the subnet.

    Usage:
        Users can specify the subnet and see the children and the proportion that is given to them.

        The command compiles a table showing:

    - ChildHotkey: The hotkey associated with the child.
    - ParentHotKey: The hotkey associated with the parent.
    - Proportion: The proportion that is assigned to them.
    - Expiration: The expiration of the hotkey.

    Example usage::

        btcli stake get_children --netuid 1

    Note:
        This command is for users who wish to see child hotkeys among different neurons (hotkeys) on the network.
    """

    @staticmethod
    def run(cli: "bittensor.cli"):
        """Get children hotkeys."""
        try:
            subtensor: "bittensor.subtensor" = bittensor.subtensor(
                config=cli.config, log_verbose=False
            )
            return GetChildrenCommand._run(cli, subtensor)
        finally:
            if "subtensor" in locals():
                subtensor.close()
                bittensor.logging.debug("closing subtensor connection")

    @staticmethod
    def _run(cli: "bittensor.cli", subtensor: "bittensor.subtensor"):
        wallet = bittensor.wallet(config=cli.config)

        # Get values if not set.
        if not cli.config.is_set("netuid"):
            cli.config.netuid = int(Prompt.ask("Enter netuid"))

        # Parse from strings
        netuid = cli.config.netuid

        children = subtensor.get_children_info(
            netuid=netuid,
        )

        GetChildrenCommand.render_table(children, netuid)

        return children

    @staticmethod
    def check_config(config: "bittensor.config"):
        if not config.is_set("wallet.name") and not config.no_prompt:
            wallet_name = Prompt.ask("Enter wallet name", default=defaults.wallet.name)
            config.wallet.name = str(wallet_name)
        if not config.is_set("wallet.hotkey") and not config.no_prompt:
            hotkey = Prompt.ask("Enter hotkey name", default=defaults.wallet.hotkey)
            config.wallet.hotkey = str(hotkey)

    @staticmethod
    def add_args(parser: argparse.ArgumentParser):
        parser = parser.add_parser(
            "get_children", help="""Get child hotkeys on subnet."""
        )
        parser.add_argument("--netuid", dest="netuid", type=int, required=False)

        bittensor.wallet.add_args(parser)
        bittensor.subtensor.add_args(parser)

    @staticmethod
    def render_table(children: list[ChildInfo], netuid: int):
        console = Console()

        # Initialize Rich table for pretty printing
        table = Table(
            show_header=True,
            header_style="bold magenta",
            border_style="green",
            style="green",
        )

        # Add columns to the table with specific styles
        table.add_column("Index", style="cyan", no_wrap=True, justify="right")
        table.add_column("ChildHotkey", style="cyan", no_wrap=True)
        table.add_column("ParentHotKeys", style="cyan", no_wrap=True)
        table.add_column("Proportion", style="cyan", no_wrap=True, justify="right")
        table.add_column("Total Stake", style="cyan", no_wrap=True, justify="right")
        table.add_column("Emissions/Day", style="cyan", no_wrap=True, justify="right")
        table.add_column("APY", style="cyan", no_wrap=True, justify="right")
        table.add_column("Take", style="cyan", no_wrap=True, justify="right")

        sum_proportion = 0.0
        sum_total_stake = 0.0
        sum_emissions_per_day = 0.0
        sum_return_per_1000 = 0.0
        sum_take = 0.0

        child_hotkeys_set = set()
        parent_hotkeys_set = set()

        if not children:
            console.print(table)
            # Summary Row
            summary = Text(
                "Total (0) | Total (0) | 0.000000 | 0.0000 | 0.0000 | 0.0000 | 0.000000",
                style="dim",
            )
            console.print(summary)

            command = f"btcli stake set_child --child <child_hotkey> --hotkey <parent_hotkey> --netuid {netuid} --proportion <float that is less than 1 >"
            console.print(f"There are currently no child hotkeys on subnet {netuid}.")
            console.print(
                f"To add a child hotkey you can run the command: [white]{command}[/white]"
            )
            return

        # Sort children by proportion (highest first)
        sorted_children = sorted(
            children.items(), key=lambda item: item[1][0].proportion, reverse=True
        )

        # Populate table with children data
        index = 1
        for child_hotkey, child_infos in sorted_children:
            for child_info in child_infos:
                table.add_row(
                    str(index),
                    child_info.child_ss58[:5] + "..." + child_info.child_ss58[-5:],
                    str(len(child_info.parents)),
                    str(u64_to_float(child_info.proportion)),
                    str(child_info.total_stake),
                    str(child_info.emissions_per_day),
                    str(
                        GetChildrenCommand.calculate_apy(child_info.return_per_1000.tao)
                    ),
                    str(child_info.take),
                )

                # Update totals and sets
                child_hotkeys_set.add(child_info.child_ss58)
                parent_hotkeys_set.update(p[1] for p in child_info.parents)
                sum_proportion += child_info.proportion
                sum_total_stake += float(child_info.total_stake)
                sum_emissions_per_day += float(child_info.emissions_per_day)
                sum_return_per_1000 += float(child_info.return_per_1000)
                sum_take += float(child_info.take)

        # Calculate averages
        total_child_hotkeys = len(child_hotkeys_set)
        total_parent_hotkeys = len(parent_hotkeys_set)
        avg_emissions_per_day = (
            sum_emissions_per_day / total_child_hotkeys if total_child_hotkeys else 0
        )
        avg_apy = (
            GetChildrenCommand.calculate_apy(sum_return_per_1000) / total_child_hotkeys
            if total_child_hotkeys
            else 0
        )

        # Print table to console
        console.print(table)

        # Add a summary row with fixed-width fields
        summary = Text(
            f"Total ({total_child_hotkeys:3}) | Total ({total_parent_hotkeys:3}) | "
            f"Total ({u64_to_float(sum_proportion):10.6f}) | Total ({sum_total_stake:10.4f}) | "
            f"Avg ({avg_emissions_per_day:10.4f}) | Avg ({avg_apy:10.4f}) | "
            f"Total ({sum_take:10.6f})",
            style="dim",
        )
        console.print(summary)

    @staticmethod
    def calculate_apy(daily_return_per_1000_tao):
        """
        Calculate the Annual Percentage Yield (APY) from the daily return per 1000 TAO.

        Args:
        daily_return_per_1000_tao (float): The daily return per 1000 TAO.

        Returns:
        float: The annual percentage yield (APY).
        """
        daily_return_rate = daily_return_per_1000_tao / 1000
        # Compounding periods per year considering 12 seconds interval generation
        compounding_periods_per_year = (365 * 24 * 60 * 60) / 12
        apy = (1 + daily_return_rate) ** compounding_periods_per_year - 1
        return apy
