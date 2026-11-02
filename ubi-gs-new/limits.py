"""Connection limits for the TCP services (each service process has its own)."""
import os, socket, threading, traceback

SOCKET_TIMEOUT = float(os.environ.get('UBIGS_SOCKET_TIMEOUT', '180'))
"""Seconds a connection may stay silent; longer than the game's keepalive window (105s)."""

MAX_CONNECTIONS = int(os.environ.get('UBIGS_MAX_CONNECTIONS', '512'))
"""Connections a service accepts at once."""

MAX_PER_IP = int(os.environ.get('UBIGS_MAX_PER_IP', '16'))
"""Connections a service accepts at once from one address (players behind one NAT share it)."""

class ConnectionLimits:
  """Counts the open connections of a service, in total and per address."""
  def __init__(self, max_total: int = MAX_CONNECTIONS, max_per_ip: int = MAX_PER_IP, timeout: float = SOCKET_TIMEOUT):
    self.max_total = max_total
    self.max_per_ip = max_per_ip
    self.timeout = timeout
    self.per_ip: dict[str, int] = {}
    self.total = 0
    self.lock = threading.Lock()

  def acquire(self, ip: str):
    """Takes a slot for a connection from the address, if there is one."""
    with self.lock:
      if self.total >= self.max_total or self.per_ip.get(ip, 0) >= self.max_per_ip:
        return False
      self.total += 1
      self.per_ip[ip] = self.per_ip.get(ip, 0) + 1
      return True

  def release(self, ip: str):
    """Frees the slot of a closed connection."""
    with self.lock:
      self.total -= 1
      self.per_ip[ip] -= 1
      if self.per_ip[ip] == 0:
        del self.per_ip[ip]

  def admit(self, clt):
    """Accepts a new `client.TcpClient` within the limits and sets its timeout, or closes it."""
    if not self.acquire(clt.addr[0]):
      print(f"Refused {clt.addr}: connection limit reached")
      try:
        clt.conn.close()
      except OSError:
        pass
      return False
    clt.conn.settimeout(self.timeout)
    return True

  def serve(self, handler, clt):
    """Thread body: runs the service's handler for an admitted client and frees its slot."""
    try:
      handler(clt)
    except socket.timeout:
      print(f"Closed {clt.addr}: silent for {self.timeout:.0f}s")
    except Exception as err:
      # one bad connection must not affect the others
      print(f"Closed {clt.addr}: {err!r}")
      traceback.print_exc()
    finally:
      self.release(clt.addr[0])

TCP = ConnectionLimits()
"""Limits of this service process."""
