import socket
import sys
import csv
import traceback
import queue
import errno
import json
from time import sleep
import os
import datetime
import random
from datetime import timedelta
import threading 
import time
import html
import cchardet

class submitty_router():
  '''
  A constructor for the standard router, set seed to a positive integer to
    make a number of router functions deterministic run-on-run.
  '''
  def __init__(self, seed=None, log_file='router_log.txt'):
    if seed != None:
      random.seed( seed )
    # Variable to keep track of how many messages we have intercepted so far.
    self.messages_intercepted = 0
    # Initialized when run is called.
    self.execution_start_time = None
    self.log_file = log_file
    self.sequence_diagram_file = 'sequence_diagram.txt'
    self.known_hosts = None
    self.ports = list()
    # Priority queue is thread safe.
    self.p_queue = queue.PriorityQueue()
    self.running = False
    self.udp_sockets = dict()

  ##################################################################################################################
  # INSTRUCTOR FUNCTIONS
  ##################################################################################################################
  
  '''
  Override this function to manipulate student messages.

  data has the following keys:
  1. sender: The node that sent the message.
  2. recipient: The node to which the message is bound
  3. port: The port on which the message was received and will be sent.
  4. message: The message that was passed.
  5. message_number: The sequence number of the message as it was received by the router.
  6. receipt_time: The time at which the message was received by the router (sent by the sender)
  7. forward_time: The time at which the message is scheduled to be forwarded by the router to the recipient.
     Defaults to be equivalent to receipt_time.
  8. time_since_test_start: how long the test has currently been running as a timedelta object. 
  9. drop_message: A boolean, which, if true, states that the message will be dropped.
     Defaults to false.
  10. diagram_label: A label for this message on the sequence diagram generated by this testcase
     Defaults to None
  '''
  def manipulate_received_message(self, data):
    return data

  def log(self, line):
    if os.path.exists(self.log_file):
      append_write = 'a' # append if already exists
    else:
        append_write = 'w' # make a new file if not
    with open(self.log_file, mode=append_write) as out_file:
      out_file.write(line + '\n')
      out_file.flush()
    print(line)
    sys.stdout.flush()


  # All messages will be passed through this function for optional decoding 
  # to string
  def sequence_diagram_message_preprocess(self, message):
    """
    Preprocess a message for sequence diagram, attempting to decode it 
    to a human-readable string.

    Parameters:
    - message(str): The message to be preprocessed.

    Returns:
    - result(str): The preprocessed message.
    """
    result = message
    encoding_prediction = cchardet.detect(message)
    confidence_threshold = 0.8
    
    if encoding_prediction['confidence'] > confidence_threshold:
      try:
        result = message.decode(encoding_prediction['encoding'])
      except UnicodeDecodeError as e:
        self.log(f"Error decoding message: {str(e)}")
      except Exception as e:
        self.log(f"Unexpected error during decoding: {str(e)}")
    else:
      self.log(f"Low confidence ({encoding_prediction['confidence']}) in detected encoding ({encoding_prediction['encoding']}). Using default decoding.")
      try:
        result = message.decode('utf-8', errors='replace')
      except UnicodeDecodeError as e:
        self.log(f"Error decoding message with default encoding (UTF-8): {str(e)}")
      except Exception as e:
        self.log(f"Unexpected error during decoding (UTF-8): {str(e)}")
    return result

  def write_sequence_file(self, obj, status, message_type):
    append_write = 'a' if os.path.exists(self.sequence_diagram_file) else 'w'

    #select the proper arrow type for the message
    if status == 'success':
      arrow = '->>' #if message_type == 'tcp' else '-->>'
    else:
      arrow = '-x' #if message_type == 'tcp' else '--x'

    sender        = obj['sender'].replace('_Actual', '')
    recipient     = obj['recipient'].replace('_Actual', '')
    message       = self.sequence_diagram_message_preprocess(obj['message'])
    diagram_label = obj['diagram_label'] if 'diagram_label' in obj else None

    with open(self.sequence_diagram_file, append_write) as outfile:
      start = f'{sender}{arrow}{recipient}'
      message_lines = []

      # newline every n characters
      newline_cadence = 24
      # most_lines_allowed (not including ending ellipsis)
      max_num_lines = 10

      message = str(message)
      for i in range(0, len(message), newline_cadence):
        if i + newline_cadence > newline_cadence*max_num_lines:
          message_lines.append('...')
          break
        message_lines.append(message[i:i+newline_cadence])

      if len(message_lines) == 1:
        outfile.write(f'{start}: {message}\n')
      else:
        str_lines = [str(x) for x in message_lines ]
        outfile.write(f'{start}: {"<br>".join(str_lines)}\n')

      if diagram_label is not None and obj['diagram_label'].strip() != '':
        outfile.write(f'Note over {sender},{recipient}: {diagram_label}\n')


  """
  {
    "alpha" : {
      ip_address : "xxx.xxx.xxx.xxx",
      "udp_start_port" : num,
      "udp_end_port" : num,
      "tcp_start_port" : num,
      "tcp_end_port" : num
    }
  }
  """
  def parse_knownhosts(self):
    with open('knownhosts.json', 'r') as infile:
      data = json.load(infile)
      self.known_hosts = data['hosts']

  def get_hostname_with_ip(self, ip_address):
    for host, details in self.known_hosts.items():
      if details['ip_address'] == ip_address:
        return host
    raise Exception(f'unknown ip {ip_address}')


  def handle_queue(self):
    while self.running:
      try:
        now = datetime.datetime.now()
        #priority queue has no peek function due to threading issues.
        #  as a result, pull it off, check it, then put it back on.
        value = self.p_queue.get_nowait()
        if value[0] <= now:
          self.forward_message(value[1])
        else:
          self.p_queue.put(value)
      except queue.Empty:
        # Sleep a thousandth of a second.
        time.sleep(.001)
        pass

  def forward_message(self, data):
    status = 'unset'
    message_type = 'unset'
    
    try:
      drop_message = data.get('drop_message', False)
      send_port = data['send_port']
      recv_port = data['recv_port']
      message = data['message']
      sock = data['socket']
      recipient = data['recipient']
      message_type = data['socket_type']
    except:
      status = 'router_error'
      self.log("An error occurred internal to the router. Please report the following error to a Submitty Administrator")
      self.log(traceback.format_exc())
      return
    
    try:
      if drop_message:
        success = "dropped"
        self.log("Choosing not to deliver message {!r} to {}".format(message, recipient))
      elif message_type == 'tcp':
        sock.sendall(message)
        self.log(f'Delivered the message {data["sender"]} -> {data["recipient"]}: {data["message"]}')
        status = 'success'
      else:
        destination_address = (recipient, int(recv_port))
        sock.sendto(message, destination_address)
        self.log(f'Delivered the message ({data["sender"]} {send_port}) -> ({recipient} {recv_port}): {data["message"]}')
        status = 'success'
    except:
      self.log('Could not deliver message {!r} to {}'.format(message,recipient))
      traceback.print_exc()
      # TODO: close the socket here?
      status = 'failure'
    self.write_sequence_file(data, status, message_type)


  def listen_for_tcp(self, recipient, recv_port):
    recipient = f'{recipient}_Actual'
    listener_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener_socket.bind(('', recv_port))
    listener_socket.listen(5)
    listener_socket.settimeout(1)

    while self.running:
      try:
        (clientsocket, address) = listener_socket.accept()
      except socket.timeout as e:
        continue

      try:
        sender = self.get_hostname_with_ip(address[0])
      except Exception:
        print(f"ERROR: we don't know the address {address[0]}")
        print(json.dumps(self.known_hosts,indent=4))
        continue

      try:
        serversocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        serversocket.connect((recipient, recv_port))
      except Exception:
        # TODO: Write to sequence diagram in this case.
        clientsocket.close()
        continue

      client_handler = threading.Thread(target=self.handle_tcp_throughput, args=(clientsocket, serversocket, sender, recipient, 'client'))
      server_handler = threading.Thread(target=self.handle_tcp_throughput, args=(serversocket, clientsocket, recipient, sender, 'server'))
      client_handler.start()
      server_handler.start()


  def handle_tcp_throughput(self, listen_socket, forward_socket, listen_name, forward_name, printable):

    disconnected = False
    while self.running and not disconnected:
      message = listen_socket.recv(1024)

      if message == b'':
        try:
          listen_socket.close()
        except socket.error:
          pass

        disconnected = True

      else: # if echo_request == False:
        recv_port = listen_socket.getsockname()[1]
        self.enqueue_message(listen_name, forward_name, recv_port, recv_port, message, forward_socket, 'tcp')


  def handle_udp_connection(self, recipient, recv_port, udp_socket):

    while self.running:
      try:
        msg, addr = udp_socket.recvfrom(1024)
      except socket.timeout as e:
        continue
      try:
        sender = self.get_hostname_with_ip(addr[0])
      except Exception:
        print(f"ERROR: we don't know this address {addr[0]}")
        continue
      send_port = addr[1]
      # TODO: Need to grab socket with the correct outgoing port, can't just use this udp_socket 
      if send_port not in self.udp_sockets:
        # We need to create and listen to a new socket.
        forward_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        forward_socket.bind(('', send_port))
        forward_socket.settimeout(1)
        self.udp_sockets[send_port] = forward_socket

        t = threading.Thread(target = self.handle_udp_connection, args=(sender, send_port, forward_socket))
        t.start()
      else:
        forward_socket = self.udp_sockets[send_port]

      self.enqueue_message(sender, f'{recipient}_Actual', send_port, recv_port, msg, forward_socket, 'udp')


  def enqueue_message(self, sender, recipient, send_port, recv_port, message, socket, socket_type):
    self.log(f'Enqueueing a new message ({sender}, {send_port})-({socket_type})-> ({recipient}, {recv_port}): {message}')
    now = datetime.datetime.now()
    data = {
      'sender' : sender,
      'recipient' : recipient, 
      'recv_port' : recv_port,
      'send_port' : send_port,
      'socket' : socket,
      'message' : message,
      'socket_type' : socket_type,
      'message_number' : self.messages_intercepted,
      'receipt_time' : now,
      'forward_time' : now,
      'time_since_test_start' : now - self.execution_start_time,
      'drop_message' : False,
      'diagram_label' : None
    }
    
    tup = self.manipulate_received_message(data)

    self.p_queue.put((data['forward_time'], data))


  def init(self):
    self.parse_knownhosts()

  def run(self):
    self.running = True
    self.execution_start_time = datetime.datetime.now()
    for host, details in self.known_hosts.items():
      # Start tcp threads
      for port in range(details['tcp_start_port'], details['tcp_end_port'] + 1):
        t = threading.Thread(target = self.listen_for_tcp, args=(host, port))
        t.start()

      # Start udp threads
      for port in range(details['udp_start_port'], details['udp_end_port'] + 1):
        print(f'Hooking up udp {host} {port}')
        udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp_socket.bind(('', port))
        udp_socket.settimeout(1)
        self.udp_sockets[port] = udp_socket
        t = threading.Thread(target = self.handle_udp_connection, args=(host, port, udp_socket))
        t.start()
    queue_thread = threading.Thread(target=self.handle_queue)
    queue_thread.start()
    queue_thread.join()
    self.running = False

if __name__ == '__main__':
  router = submitty_router()
  router.init()
  router.run()