import asyncio
import time
from typing import Union, TYPE_CHECKING

import numpy as np
from bittensor_wallet import Wallet
from bittensor_wallet.errors import KeyFileError
from numpy.typing import NDArray
from rich.prompt import Confirm
from rich.table import Table, Column
from substrateinterface.exceptions import SubstrateRequestException

from bittensor.utils import u16_normalized_float, format_error_message
from bittensor.utils.btlogging import logging
from bittensor.utils.weight_utils import (
    normalize_max_weight,
    convert_weights_and_uids_for_emit,
)

if TYPE_CHECKING:
    from bittensor.core.async_subtensor import AsyncSubtensor


async def get_limits(subtensor: "AsyncSubtensor") -> tuple[int, float]:
    # Get weight restrictions.
    maw, mwl = await asyncio.gather(
        subtensor.get_hyperparameter("MinAllowedWeights", netuid=0),
        subtensor.get_hyperparameter("MaxWeightsLimit", netuid=0),
    )
    min_allowed_weights = int(maw)
    max_weight_limit = u16_normalized_float(int(mwl))
    return min_allowed_weights, max_weight_limit


async def root_register_extrinsic(
    subtensor: "AsyncSubtensor",
    wallet: Wallet,
    wait_for_inclusion: bool = True,
    wait_for_finalization: bool = True,
    prompt: bool = False,
) -> bool:
    """Registers the wallet to root network.

    :param subtensor: The AsyncSubtensor object
    :param wallet: Bittensor wallet object.
    :param wait_for_inclusion: If set, waits for the extrinsic to enter a block before returning `True`, or returns `False` if the extrinsic fails to enter the block within the timeout.
    :param wait_for_finalization: If set, waits for the extrinsic to be finalized on the chain before returning `True`, or returns `False` if the extrinsic fails to be finalized within the timeout.
    :param prompt: If `True`, the call waits for confirmation from the user before proceeding.

    :return: `True` if extrinsic was finalized or included in the block. If we did not wait for finalization/inclusion, the response is `True`.
    """

    try:
        wallet.unlock_coldkey()
    except KeyFileError:
        logging.error("Error decrypting coldkey (possibly incorrect password)")
        return False

    logging.debug(
        f"Checking if hotkey (<blue>{wallet.hotkey_str}</blue>) is registered on root."
    )
    is_registered = await subtensor.is_hotkey_registered(
        netuid=0, hotkey_ss58=wallet.hotkey.ss58_address
    )
    if is_registered:
        logging.error(
            ":white_heavy_check_mark: <green>Already registered on root network.</green>"
        )
        return True

    logging.info(":satellite: <magenta>Registering to root network...</magenta>")
    call = await subtensor.substrate.compose_call(
        call_module="SubtensorModule",
        call_function="root_register",
        call_params={"hotkey": wallet.hotkey.ss58_address},
    )
    success, err_msg = await subtensor.sign_and_send_extrinsic(
        call,
        wallet=wallet,
        wait_for_inclusion=wait_for_inclusion,
        wait_for_finalization=wait_for_finalization,
    )

    if not success:
        logging.error(f":cross_mark: <red>Failed</red>: {err_msg}")
        time.sleep(0.5)
        return False

    # Successful registration, final check for neuron and pubkey
    else:
        uid = await subtensor.substrate.query(
            module="SubtensorModule",
            storage_function="Uids",
            params=[0, wallet.hotkey.ss58_address],
        )
        if uid is not None:
            logging.info(
                f":white_heavy_check_mark: <green>Registered with UID</green> <blue>{uid}</blue>"
            )
            return True
        else:
            # neuron not found, try again
            logging.error(":cross_mark: <red>Unknown error. Neuron not found.</red>")
            return False


async def set_root_weights_extrinsic(
    subtensor: "AsyncSubtensor",
    wallet: Wallet,
    netuids: Union[NDArray[np.int64], list[int]],
    weights: Union[NDArray[np.float32], list[float]],
    version_key: int = 0,
    wait_for_inclusion: bool = False,
    wait_for_finalization: bool = False,
    prompt: bool = False,
) -> bool:
    """Sets the given weights and values on chain for wallet hotkey account.

    :param subtensor: The AsyncSubtensor object
    :param wallet: Bittensor wallet object.
    :param netuids: The `netuid` of the subnet to set weights for.
    :param weights: Weights to set. These must be `float` s and must correspond to the passed `netuid` s.
    :param version_key: The version key of the validator.
    :param wait_for_inclusion: If set, waits for the extrinsic to enter a block before returning `True`, or returns
                              `False` if the extrinsic fails to enter the block within the timeout.
    :param wait_for_finalization: If set, waits for the extrinsic to be finalized on the chain before returning `True`,
                                  or returns `False` if the extrinsic fails to be finalized within the timeout.
    :param prompt: If `True`, the call waits for confirmation from the user before proceeding.
    :return: `True` if extrinsic was finalized or included in the block. If we did not wait for finalization/inclusion,
             the response is `True`.
    """

    async def _do_set_weights():
        call = await subtensor.substrate.compose_call(
            call_module="SubtensorModule",
            call_function="set_root_weights",
            call_params={
                "dests": weight_uids,
                "weights": weight_vals,
                "netuid": 0,
                "version_key": version_key,
                "hotkey": wallet.hotkey.ss58_address,
            },
        )
        # Period dictates how long the extrinsic will stay as part of waiting pool
        extrinsic = await subtensor.substrate.create_signed_extrinsic(
            call=call,
            keypair=wallet.coldkey,
            era={"period": 5},
        )
        response = await subtensor.substrate.submit_extrinsic(
            extrinsic,
            wait_for_inclusion=wait_for_inclusion,
            wait_for_finalization=wait_for_finalization,
        )
        # We only wait here if we expect finalization.
        if not wait_for_finalization and not wait_for_inclusion:
            return True, "Not waiting for finalization or inclusion."

        await response.process_events()
        if await response.is_success:
            return True, "Successfully set weights."
        else:
            return False, await response.error_message

    my_uid = await subtensor.substrate.query(
        "SubtensorModule", "Uids", [0, wallet.hotkey.ss58_address]
    )

    if my_uid is None:
        logging.error("Your hotkey is not registered to the root network")
        return False

    try:
        wallet.unlock_coldkey()
    except KeyFileError:
        logging.error("Error decrypting coldkey (possibly incorrect password)")
        return False

    # First convert types.
    if isinstance(netuids, list):
        netuids = np.array(netuids, dtype=np.int64)
    if isinstance(weights, list):
        weights = np.array(weights, dtype=np.float32)

    logging.debug("Fetching weight limits")
    min_allowed_weights, max_weight_limit = await get_limits(subtensor)

    # Get non zero values.
    non_zero_weight_idx = np.argwhere(weights > 0).squeeze(axis=1)
    non_zero_weights = weights[non_zero_weight_idx]
    if non_zero_weights.size < min_allowed_weights:
        raise ValueError(
            "The minimum number of weights required to set weights is {}, got {}".format(
                min_allowed_weights, non_zero_weights.size
            )
        )

    # Normalize the weights to max value.
    logging.info("Normalizing weights")
    formatted_weights = normalize_max_weight(x=weights, limit=max_weight_limit)
    logging.info(
        f"Raw weights -> Normalized weights: <blue>{weights}</blue> -> <green>{formatted_weights}</green>"
    )

    # Ask before moving on.
    if prompt:
        table = Table(
            Column("[dark_orange]Netuid", justify="center", style="bold green"),
            Column(
                "[dark_orange]Weight", justify="center", style="bold light_goldenrod2"
            ),
            expand=False,
            show_edge=False,
        )
        print("Netuid | Weight")

        for netuid, weight in zip(netuids, formatted_weights):
            table.add_row(str(netuid), f"{weight:.8f}")
            print(f"{netuid} | {weight}")

        if not Confirm.ask("\nDo you want to set these root weights?"):
            return False

    try:
        logging.info(":satellite: <magenta>Setting root weights...<magenta>")
        weight_uids, weight_vals = convert_weights_and_uids_for_emit(netuids, weights)

        success, error_message = await _do_set_weights()

        if not wait_for_finalization and not wait_for_inclusion:
            return True

        if success is True:
            logging.info(":white_heavy_check_mark: <green>Finalized</green>")
            return True
        else:
            fmt_err = format_error_message(error_message, subtensor.substrate)
            logging.error(f":cross_mark: <red>Failed</red>: {fmt_err}")
            return False

    except SubstrateRequestException as e:
        fmt_err = format_error_message(e, subtensor.substrate)
        logging.error(f":cross_mark: <red>Failed</red>: error:{fmt_err}")
        return False