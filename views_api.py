import json
from asyncio.log import logger
from http import HTTPStatus
from urllib.parse import urlparse

from fastapi import Depends, Query, Request
from lnurl.exceptions import InvalidUrl as LnurlInvalidUrl
from starlette.exceptions import HTTPException

from lnbits.core.crud import get_user
from lnbits.decorators import WalletTypeInfo, check_admin, get_key_type
from lnbits.utils.exchange_rates import currencies, get_fiat_rate_satoshis

from . import lnurlp_ext, scheduled_tasks
from .crud import (
    create_pay_link,
    delete_pay_link,
    get_pay_link,
    get_pay_links,
    update_pay_link,
    get_address_data,
)
from .models import CreatePayLinkData
from .lnurl import api_lnurl_response

# redirected from /.well-known/lnurlp
@lnurlp_ext.get("/api/v1/well-known/{username}")
async def lnaddress(username: str, request: Request):
    address_data = await get_address_data(username)
    assert address_data, "User not found"
    return await api_lnurl_response(request, address_data.id, lnaddress=True)


@lnurlp_ext.get("/api/v1/currencies")
async def api_list_currencies_available():
    return list(currencies.keys())


@lnurlp_ext.get("/api/v1/links", status_code=HTTPStatus.OK)
async def api_links(
    req: Request,
    wallet: WalletTypeInfo = Depends(get_key_type),
    all_wallets: bool = Query(False),
):
    wallet_ids = [wallet.wallet.id]

    if all_wallets:
        user = await get_user(wallet.wallet.user)
        wallet_ids = user.wallet_ids if user else []

    try:
        return [
            {**link.dict(), "lnurl": link.lnurl(req)}
            for link in await get_pay_links(wallet_ids)
        ]

    except LnurlInvalidUrl:
        raise HTTPException(
            status_code=HTTPStatus.UPGRADE_REQUIRED,
            detail="LNURLs need to be delivered over a publically accessible `https` domain or Tor.",
        )


@lnurlp_ext.get("/api/v1/links/{link_id}", status_code=HTTPStatus.OK)
async def api_link_retrieve(
    r: Request, link_id, wallet: WalletTypeInfo = Depends(get_key_type)
):
    link = await get_pay_link(link_id)

    if not link:
        raise HTTPException(
            detail="Pay link does not exist.", status_code=HTTPStatus.NOT_FOUND
        )

    if link.wallet != wallet.wallet.id:
        raise HTTPException(
            detail="Not your pay link.", status_code=HTTPStatus.FORBIDDEN
        )

    return {**link.dict(), **{"lnurl": link.lnurl(r)}}


@lnurlp_ext.post("/api/v1/links", status_code=HTTPStatus.CREATED)
@lnurlp_ext.put("/api/v1/links/{link_id}", status_code=HTTPStatus.OK)
async def api_link_create_or_update(
    data: CreatePayLinkData,
    request: Request,
    link_id=None,
    wallet: WalletTypeInfo = Depends(get_key_type),
):

    if data.min > data.max:
        raise HTTPException(
            detail="Min is greater than max.", status_code=HTTPStatus.BAD_REQUEST
        )

    if data.currency is None and (
        round(data.min) != data.min or round(data.max) != data.max or data.min < 1
    ):
        raise HTTPException(
            detail="Must use full satoshis.", status_code=HTTPStatus.BAD_REQUEST
        )

    if data.webhook_headers:
        try:
            json.loads(data.webhook_headers)
        except ValueError:
            raise HTTPException(
                detail="Invalid JSON in webhook_headers.",
                status_code=HTTPStatus.BAD_REQUEST,
            )

    if data.webhook_body:
        try:
            json.loads(data.webhook_body)
        except ValueError:
            raise HTTPException(
                detail="Invalid JSON in webhook_body.",
                status_code=HTTPStatus.BAD_REQUEST,
            )

    # database only allows int4 entries for min and max. For fiat currencies,
    # we multiply by data.fiat_base_multiplier (usually 100) to save the value in cents.
    if data.currency and data.fiat_base_multiplier:
        data.min *= data.fiat_base_multiplier
        data.max *= data.fiat_base_multiplier

    if data.success_url is not None and not data.success_url.startswith("https://"):
        raise HTTPException(
            detail="Success URL must be secure https://...",
            status_code=HTTPStatus.BAD_REQUEST,
        )

    if link_id:
        link = await get_pay_link(link_id)

        if not link:
            raise HTTPException(
                detail="Pay link does not exist.", status_code=HTTPStatus.NOT_FOUND
            )

        if link.wallet != wallet.wallet.id:
            raise HTTPException(
                detail="Not your pay link.", status_code=HTTPStatus.FORBIDDEN
            )

        link = await update_pay_link(**data.dict(), link_id=link_id)
    else:
        link = await create_pay_link(data, wallet_id=wallet.wallet.id)
    assert link
    return {**link.dict(), "lnurl": link.lnurl(request)}


@lnurlp_ext.delete("/api/v1/links/{link_id}", status_code=HTTPStatus.OK)
async def api_link_delete(link_id, wallet: WalletTypeInfo = Depends(get_key_type)):
    link = await get_pay_link(link_id)

    if not link:
        raise HTTPException(
            detail="Pay link does not exist.", status_code=HTTPStatus.NOT_FOUND
        )

    if link.wallet != wallet.wallet.id:
        raise HTTPException(
            detail="Not your pay link.", status_code=HTTPStatus.FORBIDDEN
        )

    await delete_pay_link(link_id)
    return {"success": True}


@lnurlp_ext.get("/api/v1/rate/{currency}", status_code=HTTPStatus.OK)
async def api_check_fiat_rate(currency):
    try:
        rate = await get_fiat_rate_satoshis(currency)
    except AssertionError:
        rate = None

    return {"rate": rate}


@lnurlp_ext.delete("/api/v1", status_code=HTTPStatus.OK)
async def api_stop(wallet: WalletTypeInfo = Depends(check_admin)):
    for t in scheduled_tasks:
        try:
            t.cancel()
        except Exception as ex:
            logger.warning(ex)

    return {"success": True}
