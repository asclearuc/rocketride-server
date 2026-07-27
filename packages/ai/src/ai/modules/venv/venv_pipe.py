import hmac
import os

from fastapi import WebSocket, WebSocketDisconnect

from rocketlib import debug, error, Lvl


VENV_TOKEN_ENV = 'ROCKETRIDE_VENV_TOKEN'


def _bearer(authorization: str) -> str:
    """Extract the token from an ``Authorization: Bearer <token>`` header, or ''."""
    if not authorization:
        return ''
    parts = authorization.split(' ', 1)
    if len(parts) == 2 and parts[0].lower() == 'bearer':
        return parts[1].strip()
    return ''


def _find_venv_server(pipe, channel_id: str):
    """Walk a child pipe stack for the ``venv_server`` node serving ``channel_id``."""
    node = pipe
    while node is not None:
        pipe_type = node.pipeType
        if pipe_type.logicalType == 'venv_server' and pipe_type.connConfig.get('channelId') == channel_id:
            return node
        node = node.next
    return None


async def venv_pipe(webSocket: WebSocket):
    """Route WS ``/venv/pipe`` on a venv child engine.

    The main-side round-trip ``venv`` client dials this once per boundary with a Bearer token
    and ``?channel=<forwardChannelId>`` (plus ``&return=<returnChannelId>`` when the venv
    returns data). The child authenticates the token (per-run shared secret in
    ``ROCKETRIDE_VENV_TOKEN``, inherited via env, never argv/disk) BEFORE accepting, then
    resolves the boundary's ``venv_server`` nodes from the resident source's published pipe
    stack (``app.state.target`` -- the DATA module's mechanism, not an ``ILoader``):

    - the **ingress** (matched by ``channel``) is serviced by its ``handleWebSocket`` accept
      loop, applying the forward stream (all its lanes) to the local pipeline;
    - the **egress** (matched by ``return``) has the same socket bound to it, so the engine
      driving its ``write*`` ships the venv's output (all its return lanes) back over this one
      connection (its ``callRemote`` nests inside the ingress loop's ``callLocal``).

    One socket carries both directions and all lanes, so the spliced object is never re-opened
    on main.
    """
    # §4.5: reject unauthenticated connections BEFORE completing the handshake. A bad/absent
    # token closes with 1008 (policy violation) so no socket is ever serviced.
    expected = os.environ.get(VENV_TOKEN_ENV)
    provided = _bearer(webSocket.headers.get('authorization'))
    channel_id = webSocket.query_params.get('channel')
    if not expected or not provided or not hmac.compare_digest(provided, expected) or not channel_id:
        await webSocket.close(code=1008)
        return

    # Authenticated: complete the WebSocket handshake before servicing the connection.
    await webSocket.accept()

    return_id = webSocket.query_params.get('return')

    # Reach the child's pipe stack via the resident source's published target endpoint.
    target = webSocket.app.state.target
    pipe = target.getPipe()
    in_instance = None
    out_instance = None
    try:
        in_node = _find_venv_server(pipe, channel_id)
        if in_node is None:
            raise Exception(f'no venv_server ingress found for channel "{channel_id}"')
        in_instance = in_node.pyInstance

        # A boundary that returns data binds the SAME socket to its egress node, so the
        # engine-driven return write* (all its lanes) ship back over this one connection.
        if return_id:
            out_node = _find_venv_server(pipe, return_id)
            if out_node is None:
                raise Exception(f'no venv_server egress found for return channel "{return_id}"')
            out_instance = out_node.pyInstance
            out_instance.connect(webSocket)

        # Blocking forward-ingress recv loop; the egress's callRemote nests inside it.
        in_instance.handleWebSocket(webSocket)

    except WebSocketDisconnect:
        debug(Lvl.Remoting, f'Closed venv pipe connection for channel "{channel_id}"')

    except BaseException as e:
        error(e)
        try:
            await webSocket.close()
        except Exception:
            pass

    finally:
        # Release the shared socket from both instances so a reused pooled pipe reconnects
        # cleanly on the next boundary connection.
        if in_instance is not None:
            in_instance._webSocket = None
        if out_instance is not None:
            out_instance._webSocket = None
        target.putPipe(pipe)
