import os.path
import socket
import table
import threading
import util
import struct
import select
import time
import sys
import _thread


_CONFIG_UPDATE_INTERVAL_SEC = 5

_MAX_UPDATE_MSG_SIZE = 1024
_BASE_ID = 8000
_BYTES_PER_ENTRY = 4
_PADDING = 1
_START_INDEX = 2
_MOVE_TWO_BYTES = 2
_MOVE_FOUR_BYTES = 4


def _ToPort(router_id):
  return _BASE_ID + router_id

def _ToRouterId(port):
  return port - _BASE_ID

def _ToStop(entry_count):
  return entry_count * _BYTES_PER_ENTRY + _PADDING

class Router:
  def __init__(self, config_filename):
    # ForwardingTable has 3 columns (DestinationId,NextHop,Cost). It's
    # threadsafe.
    self._forwarding_table = table.ForwardingTable()
    # Config file has router_id, neighbors, and link cost to reach
    # them.
    self._config_filename = config_filename
    self._router_id = None
    # Socket used to send/recv update messages (using UDP).
    self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    self._lock = threading.Lock()
    # a list of (neighbors, next hop, cost) example [(1,1,0), (2,2,9), (3,3,9)]
    self._curr_neighbor_cost = []
    self._start_time = None
    self._call_counter = 1
    self._last_msg_sent = None
    self._fired_threads = []

  def start(self):
    # Start a periodic closure to update config.
    self._config_updater = util.PeriodicClosure(
        self.load_config, _CONFIG_UPDATE_INTERVAL_SEC)
    self._start_time = time.time()
    self._config_updater.start()
    while True:
      self.listener_thread()

  def stop(self):
    print("Entering stop")
    if self._config_updater:
      self._config_updater.stop()
    self._socket.close()
    self.status()
    sys.exit()

  def load_config(self):
    """
    If the forwarding table has not been initialized, initializes the router's forwarding table and then sends
    the initial forwarding table to all its neighbors.

    Regardless of whether the forwarding table has been initialized, the following will occur:
    Update the forwarding table if there is any link cost change to a neighbor.
    Update the forwarding table if receive a distance vector from a neighbor.
    If table is updated, send updated forwarding table to neighbors.

    :return: None
    """
    assert os.path.isfile(self._config_filename)
    with open(self._config_filename, 'r') as f:
      router_id = int(f.readline().strip())
      self.init_router_id(router_id)
      self.greeting(router_id)
      self.init_fwd_tbl(f)
    self.send_update_msg_to_neighbors()
    self.status()

  def init_router_id(self, router_id):
    if not self._router_id:
      self._router_id = router_id
      print("Binding socket to localhost and port: ", _ToPort(router_id), "...")
      try:
        self._socket.bind(('localhost', _ToPort(router_id)))
      except socket.error:
        print("Failure to bind socket")
        sys.exit()

  def init_fwd_tbl(self, f):
    print("Initializing forwarding table...")
    if not self.is_fwd_table_initialized():
      with self._lock:
        self._curr_neighbor_cost = []
        for line in f:
          line = line.strip('\n').split(',')
          entry = (int(line[0]), int(line[0]), int(line[1]))
          self._curr_neighbor_cost.append(entry)
        self._curr_neighbor_cost.append((self._router_id, self._router_id, 0))
        print("The forwarding table will be initialized to: ", self._curr_neighbor_cost)
        self._forwarding_table.reset(self._curr_neighbor_cost)
    else:
      print("Forwarding table already initialized.")
      print("Updating fwd tbl with most current config file...")
      self.update_fwd_tbl_with_config_file(f)

  def is_fwd_table_initialized(self):
    return self._forwarding_table.size() > 0

  def send_update_msg_to_neighbors(self):
    """
    :param msg: Bytes object representation of a node's distance vector; the format of the message is
    "Entry count, id_no, cost, ..., id_no, cost". Upon initialization, the router sends its complete
    forwarding table. But later messages will only send updated entries of its forwarding table, not
    the entire table.
    :return: None or Error
    """
    msg = self.convert_fwd_table_to_bytes_msg()
    try:
      sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except socket.error:
      print("Failure to create socket")
      sys.exit()
    with self._lock:
      for tup in self._curr_neighbor_cost:
        port = _ToPort(tup[0])
        if tup[0] != self._router_id:
          sock.sendto(msg, ('localhost', port))
    sock.close()

  def convert_fwd_table_to_bytes_msg(self):
    """
    :param snapshot: List of Tuples (id, next_hop, cost)
    :return: Bytes object representation of a forwarding table; the format of the message is
    "Entry count, id_no, cost, ..., id_no, cost"
    """
    snapshot = list(filter(lambda x: x[0] == x[1], self._forwarding_table.snapshot()))
    entry_count = len(snapshot)
    msg = bytearray()
    msg.extend(struct.pack("!h", entry_count))
    list_msg = []
    for entry in snapshot:
      dest, next_hop, cost = entry
      list_msg.append((dest, cost))
      msg.extend(struct.pack("!hh", dest, cost))
    with self._lock:
      self._last_msg_sent = list_msg
    return msg

  def update_fwd_tbl_with_config_file(self, f):
    old_curr_neighbor_cost_dict = self.curr_neighbor_cost_to_dict()
    # Only update the forwarding table if there was a link cost change
    if self.update_curr_neighbor_cost(f):
      new_curr_neighbor_cost_dict = self.curr_neighbor_cost_to_dict()
      snapshot = self._forwarding_table.snapshot()
      new_snapshot = []
      for route in snapshot:
        dest = route[0]
        # Route from origin to origin, which is a cost of 0; DO NOT CHANGE
        if route[2] == 0:
          new_snapshot.append(route)
        # Route to indirect routers (i.e. non-neighbors); DO NOT CHANGE
        # TODO: Must update indirect route for any neighbors with cost change
        elif dest not in (old_curr_neighbor_cost_dict and new_curr_neighbor_cost_dict):
          new_snapshot.append(route)
        # Route to direct routers; MUST UPDATE
        else:
          self.update_route(route, old_curr_neighbor_cost_dict, new_curr_neighbor_cost_dict, new_snapshot)
      self._forwarding_table.reset(new_snapshot)

  def curr_neighbor_cost_to_dict(self):
    with self._lock:
      curr_neighbor_cost = self._curr_neighbor_cost
    return {x[0]: x[2] for x in curr_neighbor_cost}

  def update_curr_neighbor_cost(self, f):
    # only updates the link cost of current neighbors, DOES NOT ADD NEW NEIGHBORS FROM A NEW CONFIG FILE
    # TODO: extend the future to allow adding or subtracting neighbors to a router's network via new config files
    config_neighbor_cost_dict = self.config_to_dict(f)
    cost_change = False
    with self._lock:
      new_curr_neighbor_cost = []
      for neighbor in self._curr_neighbor_cost:
        if self.is_link_cost_change(neighbor, config_neighbor_cost_dict):
          cost_change = True
          new_curr_neighbor_cost.append((neighbor[0], neighbor[1], config_neighbor_cost_dict[neighbor[0]]))
        else:
          # Either the neighbor cost did not change
          # Or the neighbor was not present in the config file (which means we do not remove neighbors from network)
          # Thus, no update needed. We retain the info.
          new_curr_neighbor_cost.append(neighbor)
      # Only update our copy of the current config file if any current link costs changed
      if cost_change:
        self._curr_neighbor_cost = new_curr_neighbor_cost
    return cost_change

  def config_to_dict(self, config_file):
    config_file_dict = {}
    for line in config_file:
      line = line.strip("\n")
      list_line = line.split(",")
      config_file_dict[int(list_line[0])] = int(list_line[1])
    return config_file_dict

  def is_link_cost_change(self, neighbor, config_neighbor_cost_dict):
    neighbor_id = neighbor[0]
    curr_neighbor_id_cost = neighbor[2]
    return (neighbor_id in config_neighbor_cost_dict and
            curr_neighbor_id_cost != config_neighbor_cost_dict[neighbor_id])

  def update_route(self, route, old_curr_neighbor_cost_dict, new_curr_neighbor_cost_dict, new_snapshot):
    # TODO: Only updates direct routes. Does not consider indirect routes that have neighbors who have changed costs
    dest = route[0]
    next_hop = route[1]
    # Adds any new DIRECT routes (i.e. new routers in the network) to the forwarding table
    if self.is_new_direct_route(route, new_curr_neighbor_cost_dict):
      new_snapshot.append((dest, next_hop, new_curr_neighbor_cost_dict[dest]))
    # Direct route has a changed cost
    elif self.is_existing_direct_route_cost_changed(route, old_curr_neighbor_cost_dict, new_curr_neighbor_cost_dict):
      new_snapshot.append((dest, next_hop, new_curr_neighbor_cost_dict[dest]))
    # Direct route has NOT cost changed
    else:
      new_snapshot.append(route)

  def is_new_direct_route(self, route, neighbor_cost_dict):
    return route[0] in neighbor_cost_dict and route[0] == route[1]

  def is_existing_direct_route_cost_changed(self, route, old_curr_link_cost_dict, new_curr_link_cost_dict):
    next_hop = route[1]
    dest = route[0]
    # Redundant test to ensure that a current direct route has changed its cost
    return (dest in old_curr_link_cost_dict and
            dest in new_curr_link_cost_dict and
            next_hop in old_curr_link_cost_dict and
            next_hop in new_curr_link_cost_dict and
            old_curr_link_cost_dict[next_hop] != new_curr_link_cost_dict[next_hop])

  # TODO: Reconsider its use; not currently used
  def get_updated_indirect_route(self, route, old_curr_link_cost_dict, new_curr_link_cost_dict):
    dest = route[0]
    next_hop = route[1]
    dest_total_cost_old = route[2]

    cost_to_dest_via_next_hop = dest_total_cost_old - old_curr_link_cost_dict[next_hop]
    dest_indirect_cost = cost_to_dest_via_next_hop + new_curr_link_cost_dict[next_hop]

    dest_direct_cost = new_curr_link_cost_dict[dest]
    if dest_direct_cost <= dest_indirect_cost:
      return dest, dest, dest_direct_cost
    else:
      return dest, next_hop, dest_indirect_cost

  def listener_thread(self):
    msg, addr = self._socket.recvfrom(_MAX_UPDATE_MSG_SIZE)
    srv = threading.Thread(target=self.process_packet, args=(msg,))
    srv.start()
    with self._lock:
      self._fired_threads.append(srv)

  def process_packet(self, msg):
    try:
      entry_count = int.from_bytes(msg[0:_START_INDEX], byteorder='big')
      dist_vector = []
      for index in range(_START_INDEX, _ToStop(entry_count), _BYTES_PER_ENTRY):
        tup = self.get_dest_cost(msg, index)
        dist_vector.append(tup)
      self.update_fwd_table(dist_vector)
      self.send_update_msg_to_neighbors()
      return 0
    except KeyboardInterrupt:
      print("Ctrl-C signal arrived in thread")
      _thread.interrupt_main()

  def get_dest_cost(self, msg, index):
    """
    Decodes the destination and cost of a distance vector entry from a router's update message
    :param msg: the UDP message (byte object) sent by a router
    :param index: the current index of the msg
    :return: Tuple in the form of (dest, cost)
    """
    dest = int.from_bytes(msg[index:index + _MOVE_TWO_BYTES], byteorder='big')
    cost = int.from_bytes(msg[index + _MOVE_TWO_BYTES:index + _MOVE_FOUR_BYTES], byteorder='big')
    return dest, cost

  def update_fwd_table(self, dist_vector):
    """
    Updates the router's forwarding table based on some distance vector either from config_file or neighbor.
    Returns a list of entries that were changed; otherwise None
    :param dist_vector: List of Tuples (id_no, cost), also could be None
    :return: List of Tuples (id, next_hop, cost)
    """
    try:
      neighbor = self.get_source_node(dist_vector)
      curr_neighbor_cost_dict = self.curr_neighbor_cost_to_dict()
      neighbor_cost = curr_neighbor_cost_dict[neighbor]
      dist_vector = list(filter(lambda x: x[0] != neighbor, dist_vector))
      dist_vector_dict = {x[0]: x[1] for x in dist_vector}
      snapshot = self._forwarding_table.snapshot()
      new_snapshot = [route for route in snapshot if route[0] not in dist_vector_dict]
      fwd_tbl_dict = {x[0]: (x[1], x[2]) for x in self._forwarding_table.snapshot()}

      for dv_entry in dist_vector:
        dest = dv_entry[0]
        new_route_cost = neighbor_cost + dv_entry[1]
        # New route to a new destination; add to routing table;
        if dest not in fwd_tbl_dict:
          new_snapshot.append((dest, neighbor, new_route_cost))
        # Evaluate whether current route is better than the route from our neighbor
        # Our current route can be direct or indirect; this cost must come from the fwd table and curr config
        else:
          if self.is_direct_route(dest, fwd_tbl_dict):
            self.handle_route(new_route_cost, curr_neighbor_cost_dict[dest], dest, neighbor, dest, new_snapshot)
          elif self.is_indirect_and_not_neighbor_as_hop(fwd_tbl_dict[dest][0], neighbor):
            self.handle_route(new_route_cost, fwd_tbl_dict[dest][1], dest, neighbor, fwd_tbl_dict[dest][0])
          # Our current route is via neighbor; thus we must update the route to the neighbor
          else:
            new_snapshot.append((dest, neighbor, new_route_cost))
      print("Updated Fwd Table: ", new_snapshot)
      self._forwarding_table.reset(new_snapshot)
    except:
      print("We should not see this message because the distance vector must come from a neighbor.")

  def get_source_node(self, dist_vector):
    """
    :param dist_vector:
    :return: An integer representing a source node OR none
    """
    ret = list(filter(lambda x: x[1] == 0, dist_vector))
    if len(ret) == 1:
      return ret[0][0]
    else:
      raise Exception("Router failed to send a complete DV table.")

  def is_direct_route(self, dest, fwd_tbl_dict):
    return dest == fwd_tbl_dict[dest][0]

  def is_indirect_and_not_neighbor_as_hop(self, next_hop, neighbor):
    return next_hop != neighbor

  def handle_route(self, new_route_cost, old_route_cost, dest, neighbor_as_hop, next_hop, new_snapshot):
    if new_route_cost < old_route_cost:
      new_snapshot.append((dest, neighbor_as_hop, new_route_cost))
    else:
      new_snapshot.append((dest, next_hop, old_route_cost))

  def greeting(self, router_id):
    print("Router id: ", router_id)
    print("CALL COUNTER:", self._call_counter, "..............")
    self._call_counter += 1

  def status(self):
    print()
    print("CURRENT FORWARDING TABLE OF ROUTER ", self._router_id)
    print("TABLE SIZE: ", len(self._forwarding_table.snapshot()), '\n')
    print(self._forwarding_table.snapshot(), '\n')
    # print("THIS IS THE CURRENT CONFIG FILE")
    # with self._lock:
    #   print(self._curr_neighbor_cost)
    # with self._lock:
      # print("LAST MESSAGE SENT: ")
      # print(self._last_msg_sent)
    elapsed = time.time() - self._start_time
    print("ELAPSED TIME: ", elapsed, '\n')
