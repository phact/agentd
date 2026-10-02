//! agentd-net: the network card of an agentd libkrun sandbox.
//!
//!     agentd-net --frames PATH --streams PATH [--gateway 10.211.0.1] [--prefix 24] [--fake-net 10.212.0.0]
//!
//! The microVM's virtio-net device (libkrun `krun_add_net_unixstream`) connects
//! to `--frames` and exchanges Ethernet frames, each prefixed with its length
//! (4 bytes, big-endian; the passt/qemu stream protocol). This process runs a
//! user-space TCP/IP stack (smoltcp) on the gateway address and:
//!
//!   * answers ARP for the gateway;
//!   * answers DNS on the gateway with a fake IP per name (from `--fake-net`),
//!     so every connection is known by hostname and the sandbox never does
//!     real DNS;
//!   * completes every TCP connection the guest opens, to any address, and
//!     hands it to agentd: a new connection to `--streams` per guest
//!     connection, starting with one JSON line
//!     `{"v":1,"src":"10.211.0.2:40000","dst":"10.212.0.5","port":443,"host":"example.com"}`.
//!     agentd answers with one byte to accept (then the connection's bytes
//!     flow both ways), or closes the stream to refuse (the guest's connection
//!     is reset). Nothing from the guest is sent before the answer, so agentd
//!     can switch the stream to TLS without racing buffered bytes;
//!   * drops other UDP, ICMP and IPv6.
//!
//! It holds no policy and no secrets. It exits when its stdin closes (agentd
//! went away) or when the VM disconnects.

use std::collections::{HashMap, VecDeque};
use std::sync::Arc;
use std::time::Duration;

use smoltcp::iface::{Config, Interface, SocketHandle, SocketSet};
use smoltcp::phy::{Device, DeviceCapabilities, Medium, RxToken, TxToken};
use smoltcp::socket::{tcp, udp};
use smoltcp::time::Instant;
use smoltcp::wire::{
    EthernetAddress, EthernetFrame, EthernetProtocol, HardwareAddress, IpAddress, IpCidr, IpEndpoint,
    IpListenEndpoint, IpProtocol, Ipv4Address, Ipv4Packet, TcpPacket,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;
use tokio::sync::{mpsc, Semaphore};
use tokio::task::JoinHandle;

const GATEWAY_MAC: [u8; 6] = [0x02, 0x61, 0x67, 0x65, 0x6e, 0x01];
const MAX_FRAME: usize = 65_536;
const TCP_BUFFER: usize = 256 * 1024;
const CREDIT: usize = 1 << 20; // bytes from agentd buffered per connection before reading pauses
const CHUNK: usize = 64 * 1024;

// --------------------------------------------------------------------------
// Arguments
// --------------------------------------------------------------------------

struct Args {
    frames: String,
    streams: String,
    gateway: Ipv4Address,
    prefix: u8,
    fake_net: Ipv4Address,
}

fn parse_args() -> Result<Args, String> {
    let mut frames = None;
    let mut streams = None;
    let mut gateway = Ipv4Address::new(10, 211, 0, 1);
    let mut prefix = 24u8;
    let mut fake_net = Ipv4Address::new(10, 212, 0, 0);
    let mut it = std::env::args().skip(1);
    while let Some(flag) = it.next() {
        let value = it.next().ok_or_else(|| format!("missing value for {flag}"))?;
        match flag.as_str() {
            "--frames" => frames = Some(value),
            "--streams" => streams = Some(value),
            "--gateway" => gateway = value.parse().map_err(|_| format!("bad --gateway {value}"))?,
            "--prefix" => prefix = value.parse().map_err(|_| format!("bad --prefix {value}"))?,
            "--fake-net" => fake_net = value.parse().map_err(|_| format!("bad --fake-net {value}"))?,
            _ => return Err(format!("unknown flag {flag}")),
        }
    }
    Ok(Args {
        frames: frames.ok_or("--frames is required")?,
        streams: streams.ok_or("--streams is required")?,
        gateway,
        prefix,
        fake_net,
    })
}

// --------------------------------------------------------------------------
// The device: frame queues between the VM socket and smoltcp
// --------------------------------------------------------------------------

#[derive(Default)]
struct Queues {
    rx: VecDeque<Vec<u8>>,
    tx: VecDeque<Vec<u8>>,
}

struct Rx(Vec<u8>);
struct Tx<'a>(&'a mut VecDeque<Vec<u8>>);

impl RxToken for Rx {
    fn consume<R, F: FnOnce(&[u8]) -> R>(self, f: F) -> R {
        f(&self.0)
    }
}

impl TxToken for Tx<'_> {
    fn consume<R, F: FnOnce(&mut [u8]) -> R>(self, len: usize, f: F) -> R {
        let mut buf = vec![0u8; len];
        let r = f(&mut buf);
        self.0.push_back(buf);
        r
    }
}

impl Device for Queues {
    type RxToken<'a> = Rx;
    type TxToken<'a> = Tx<'a>;

    fn receive(&mut self, _t: Instant) -> Option<(Self::RxToken<'_>, Self::TxToken<'_>)> {
        let frame = self.rx.pop_front()?;
        Some((Rx(frame), Tx(&mut self.tx)))
    }

    fn transmit(&mut self, _t: Instant) -> Option<Self::TxToken<'_>> {
        Some(Tx(&mut self.tx))
    }

    fn capabilities(&self) -> DeviceCapabilities {
        let mut caps = DeviceCapabilities::default();
        caps.medium = Medium::Ethernet;
        caps.max_transmission_unit = 1514;
        caps
    }
}

// --------------------------------------------------------------------------
// Fake-IP DNS
// --------------------------------------------------------------------------

struct Names {
    base: u32,
    next: u32,
    by_name: HashMap<String, Ipv4Address>,
    by_ip: HashMap<Ipv4Address, String>,
}

impl Names {
    fn new(base: Ipv4Address) -> Self {
        Names { base: u32::from(base), next: 1, by_name: HashMap::new(), by_ip: HashMap::new() }
    }

    fn ip_for(&mut self, name: &str) -> Ipv4Address {
        let name = name.trim_end_matches('.').to_ascii_lowercase();
        if let Some(ip) = self.by_name.get(&name) {
            return *ip;
        }
        let ip = Ipv4Address::from(self.base + self.next);
        self.next = if self.next >= 0xfffe { 1 } else { self.next + 1 };
        if let Some(old) = self.by_ip.insert(ip, name.clone()) {
            self.by_name.remove(&old); // the pool wrapped
        }
        self.by_name.insert(name, ip);
        ip
    }

    fn name_of(&self, ip: Ipv4Address) -> Option<&str> {
        self.by_ip.get(&ip).map(|s| s.as_str())
    }
}

fn dns_answer(query: &[u8], names: &mut Names) -> Option<Vec<u8>> {
    use simple_dns::rdata::{RData, A};
    use simple_dns::{Packet, PacketFlag, ResourceRecord, CLASS, QTYPE, TYPE};

    let packet = Packet::parse(query).ok()?;
    let mut reply = Packet::new_reply(packet.id());
    reply.set_flags(PacketFlag::RECURSION_AVAILABLE | PacketFlag::RECURSION_DESIRED);
    for q in &packet.questions {
        reply.questions.push(q.clone());
        if q.qtype == QTYPE::TYPE(TYPE::A) {
            let ip = names.ip_for(&q.qname.to_string());
            reply.answers.push(ResourceRecord::new(
                q.qname.clone(),
                CLASS::IN,
                60,
                RData::A(A { address: u32::from(ip) }),
            ));
        }
        // AAAA and everything else: no answers (NOERROR, empty): IPv4 only.
    }
    reply.build_bytes_vec().ok()
}

// --------------------------------------------------------------------------
// Connections handed to agentd
// --------------------------------------------------------------------------

enum Event {
    Data(u64, Vec<u8>),  // agentd -> guest
    Eof(u64),            // agentd closed its side
    Failed(u64),         // couldn't reach agentd
}

struct Conn {
    handle: SocketHandle,
    tuple: (Ipv4Address, u16, Ipv4Address, u16),
    to_agentd: Option<mpsc::Sender<Vec<u8>>>,
    pending: VecDeque<u8>,
    credits: Arc<Semaphore>,
    agentd_eof: bool,
    closing: bool,
    task: JoinHandle<()>,
}

async fn conn_task(
    id: u64,
    header: String,
    streams: String,
    mut from_guest: mpsc::Receiver<Vec<u8>>,
    events: mpsc::UnboundedSender<Event>,
    credits: Arc<Semaphore>,
) {
    let stream = match UnixStream::connect(&streams).await {
        Ok(s) => s,
        Err(_) => {
            let _ = events.send(Event::Failed(id));
            return;
        }
    };
    let (mut r, mut w) = stream.into_split();
    if w.write_all(header.as_bytes()).await.is_err() {
        let _ = events.send(Event::Failed(id));
        return;
    }
    // agentd's verdict: one byte to accept, EOF to refuse.
    let mut verdict = [0u8; 1];
    match r.read(&mut verdict).await {
        Ok(1) => {}
        _ => {
            let _ = events.send(Event::Failed(id));
            return;
        }
    }
    let writer = tokio::spawn(async move {
        while let Some(chunk) = from_guest.recv().await {
            if w.write_all(&chunk).await.is_err() {
                return;
            }
        }
        let _ = w.shutdown().await; // the guest finished sending
    });
    let mut buf = vec![0u8; CHUNK];
    loop {
        let n = match r.read(&mut buf).await {
            Ok(0) | Err(_) => break,
            Ok(n) => n,
        };
        match credits.acquire_many(n as u32).await {
            Ok(permit) => permit.forget(),
            Err(_) => break,
        }
        if events.send(Event::Data(id, buf[..n].to_vec())).is_err() {
            break;
        }
    }
    let _ = events.send(Event::Eof(id));
    let _ = writer.await;
}

// --------------------------------------------------------------------------
// The VM link
// --------------------------------------------------------------------------

async fn read_frames(mut r: tokio::net::unix::OwnedReadHalf, frames: mpsc::Sender<Vec<u8>>) {
    let mut len = [0u8; 4];
    loop {
        if r.read_exact(&mut len).await.is_err() {
            return;
        }
        let n = u32::from_be_bytes(len) as usize;
        if n == 0 || n > MAX_FRAME {
            return; // not a frame: the VM link is broken
        }
        let mut frame = vec![0u8; n];
        if r.read_exact(&mut frame).await.is_err() || frames.send(frame).await.is_err() {
            return;
        }
    }
}

/// A new connection attempt: an IPv4 TCP SYN (without ACK) from the guest.
fn syn_tuple(frame: &[u8]) -> Option<(Ipv4Address, u16, Ipv4Address, u16)> {
    let eth = EthernetFrame::new_checked(frame).ok()?;
    if eth.ethertype() != EthernetProtocol::Ipv4 {
        return None;
    }
    let ip = Ipv4Packet::new_checked(eth.payload()).ok()?;
    if ip.next_header() != IpProtocol::Tcp {
        return None;
    }
    let tcp = TcpPacket::new_checked(ip.payload()).ok()?;
    if tcp.syn() && !tcp.ack() {
        Some((ip.src_addr(), tcp.src_port(), ip.dst_addr(), tcp.dst_port()))
    } else {
        None
    }
}

fn smol_now() -> Instant {
    Instant::now()
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let args = match parse_args() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("agentd-net: {e}");
            std::process::exit(2);
        }
    };
    if let Err(e) = run(args).await {
        eprintln!("agentd-net: {e}");
        std::process::exit(1);
    }
}

async fn run(args: Args) -> std::io::Result<()> {
    // The VM connects to us.
    let _ = std::fs::remove_file(&args.frames);
    let listener = tokio::net::UnixListener::bind(&args.frames)?;
    let mut stdin = tokio::io::stdin();
    let mut parent_gone = [0u8; 1];
    let vm = tokio::select! {
        r = listener.accept() => r?.0,
        _ = stdin.read(&mut parent_gone) => return Ok(()),
    };
    let _ = std::fs::remove_file(&args.frames);
    let (vm_r, mut vm_w) = vm.into_split();
    let (frame_tx, mut frame_rx) = mpsc::channel::<Vec<u8>>(256);
    tokio::spawn(read_frames(vm_r, frame_tx));
    let (parent_tx, mut parent_rx) = mpsc::channel::<()>(1);
    tokio::spawn(async move {
        let mut buf = [0u8; 64];
        let mut stdin = tokio::io::stdin();
        while let Ok(n) = stdin.read(&mut buf).await {
            if n == 0 {
                break;
            }
        }
        let _ = parent_tx.send(()).await;
    });

    let mut device = Queues::default();
    let mut config = Config::new(HardwareAddress::Ethernet(EthernetAddress(GATEWAY_MAC)));
    config.random_seed = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(1);
    let mut iface = Interface::new(config, &mut device, smol_now());
    iface.update_ip_addrs(|addrs| {
        addrs.push(IpCidr::new(IpAddress::Ipv4(args.gateway), args.prefix)).ok();
    });
    // Accept packets for any destination: every address routes "via" ourselves.
    iface.routes_mut().add_default_ipv4_route(args.gateway).ok();
    iface.set_any_ip(true);

    let mut sockets = SocketSet::new(vec![]);
    let dns = {
        let rx = udp::PacketBuffer::new(vec![udp::PacketMetadata::EMPTY; 64], vec![0; 64 * 1024]);
        let tx = udp::PacketBuffer::new(vec![udp::PacketMetadata::EMPTY; 64], vec![0; 64 * 1024]);
        let mut socket = udp::Socket::new(rx, tx);
        socket
            .bind(IpListenEndpoint { addr: Some(IpAddress::Ipv4(args.gateway)), port: 53 })
            .map_err(|e| std::io::Error::other(format!("dns bind: {e}")))?;
        sockets.add(socket)
    };
    let mut names = Names::new(args.fake_net);
    let mut conns: HashMap<u64, Conn> = HashMap::new();
    let mut by_tuple: HashMap<(Ipv4Address, u16, Ipv4Address, u16), u64> = HashMap::new();
    let mut next_id: u64 = 1;
    let (ev_tx, mut ev_rx) = mpsc::unbounded_channel::<Event>();

    loop {
        let delay = iface
            .poll_delay(smol_now(), &sockets)
            .map(|d| Duration::from_micros(d.total_micros()))
            .unwrap_or(Duration::from_millis(500))
            .min(Duration::from_millis(500));

        let mut new_frames = Vec::new();
        let mut events = Vec::new();
        tokio::select! {
            f = frame_rx.recv() => match f {
                Some(f) => new_frames.push(f),
                None => return Ok(()), // the VM went away
            },
            e = ev_rx.recv() => if let Some(e) = e { events.push(e) },
            _ = parent_rx.recv() => return Ok(()),
            _ = tokio::time::sleep(delay) => {}
        }
        while let Ok(f) = frame_rx.try_recv() {
            new_frames.push(f);
        }
        while let Ok(e) = ev_rx.try_recv() {
            events.push(e);
        }

        // New guest connections get a listening socket before smoltcp sees their SYN.
        for frame in new_frames {
            if let Some(tuple) = syn_tuple(&frame) {
                if !by_tuple.contains_key(&tuple) {
                    let (src, sport, dst, dport) = tuple;
                    let mut socket = tcp::Socket::new(
                        tcp::SocketBuffer::new(vec![0; TCP_BUFFER]),
                        tcp::SocketBuffer::new(vec![0; TCP_BUFFER]),
                    );
                    socket.set_nagle_enabled(false);
                    socket.set_keep_alive(Some(smoltcp::time::Duration::from_secs(60)));
                    if socket
                        .listen(IpListenEndpoint { addr: Some(IpAddress::Ipv4(dst)), port: dport })
                        .is_ok()
                    {
                        let handle = sockets.add(socket);
                        let id = next_id;
                        next_id += 1;
                        let host = names.name_of(dst).map(str::to_owned);
                        let header = serde_json::json!({
                            "v": 1, "src": format!("{src}:{sport}"), "dst": dst.to_string(),
                            "port": dport, "host": host,
                        })
                        .to_string()
                            + "\n";
                        let (to_agentd, from_guest) = mpsc::channel::<Vec<u8>>(64);
                        let credits = Arc::new(Semaphore::new(CREDIT));
                        let task = tokio::spawn(conn_task(
                            id,
                            header,
                            args.streams.clone(),
                            from_guest,
                            ev_tx.clone(),
                            credits.clone(),
                        ));
                        conns.insert(
                            id,
                            Conn {
                                handle,
                                tuple,
                                to_agentd: Some(to_agentd),
                                pending: VecDeque::new(),
                                credits,
                                agentd_eof: false,
                                closing: false,
                                task,
                            },
                        );
                        by_tuple.insert(tuple, id);
                    }
                }
            }
            device.rx.push_back(frame);
        }

        for e in events {
            match e {
                Event::Data(id, data) => {
                    if let Some(c) = conns.get_mut(&id) {
                        c.pending.extend(data);
                    }
                }
                Event::Eof(id) => {
                    if let Some(c) = conns.get_mut(&id) {
                        c.agentd_eof = true;
                    }
                }
                Event::Failed(id) => {
                    if let Some(c) = conns.get_mut(&id) {
                        sockets.get_mut::<tcp::Socket>(c.handle).abort();
                        c.closing = true;
                    }
                }
            }
        }

        iface.poll(smol_now(), &mut device, &mut sockets);

        // DNS
        {
            let socket = sockets.get_mut::<udp::Socket>(dns);
            while let Ok((query, meta)) = socket.recv() {
                let peer: IpEndpoint = meta.endpoint;
                if let Some(answer) = dns_answer(query, &mut names) {
                    let _ = socket.send_slice(&answer, peer);
                }
            }
        }

        // Move bytes between smoltcp sockets and agentd connections.
        let mut done = Vec::new();
        for (id, c) in conns.iter_mut() {
            let socket = sockets.get_mut::<tcp::Socket>(c.handle);
            // guest -> agentd, while the channel has room (else TCP's window closes)
            if let Some(tx) = &c.to_agentd {
                while socket.can_recv() {
                    let permit = match tx.try_reserve() {
                        Ok(p) => p,
                        Err(mpsc::error::TrySendError::Full(_)) => break,
                        Err(mpsc::error::TrySendError::Closed(_)) => {
                            socket.abort();
                            c.closing = true;
                            break;
                        }
                    };
                    let chunk = socket.recv(|b| {
                        let n = b.len().min(CHUNK);
                        (n, b[..n].to_vec())
                    });
                    match chunk {
                        Ok(chunk) if !chunk.is_empty() => permit.send(chunk),
                        _ => break,
                    }
                }
                if !socket.may_recv() && socket.recv_queue() == 0 && socket.state() != tcp::State::Listen
                    && socket.state() != tcp::State::SynReceived
                {
                    c.to_agentd = None; // the guest sent FIN: tell agentd
                }
            }
            // agentd -> guest
            while !c.pending.is_empty() && socket.can_send() {
                let (a, b) = c.pending.as_slices();
                let slice = if a.is_empty() { b } else { a };
                match socket.send_slice(slice) {
                    Ok(0) | Err(_) => break,
                    Ok(n) => {
                        c.pending.drain(..n);
                        c.credits.add_permits(n);
                    }
                }
            }
            if c.agentd_eof && c.pending.is_empty() && !c.closing {
                socket.close();
                c.closing = true;
            }
            match socket.state() {
                tcp::State::Closed | tcp::State::TimeWait => done.push(*id),
                _ => {}
            }
        }
        for id in done {
            if let Some(c) = conns.remove(&id) {
                c.task.abort();
                sockets.remove(c.handle);
                by_tuple.remove(&c.tuple);
            }
        }

        iface.poll(smol_now(), &mut device, &mut sockets);
        while let Some(frame) = device.tx.pop_front() {
            vm_w.write_all(&(frame.len() as u32).to_be_bytes()).await?;
            vm_w.write_all(&frame).await?;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fake_ips_are_stable_and_reversible() {
        let mut n = Names::new(Ipv4Address::new(10, 212, 0, 0));
        let a = n.ip_for("Example.COM.");
        assert_eq!(a, Ipv4Address::new(10, 212, 0, 1));
        assert_eq!(n.ip_for("example.com"), a);
        assert_eq!(n.name_of(a), Some("example.com"));
        assert_eq!(n.ip_for("other.org"), Ipv4Address::new(10, 212, 0, 2));
    }

    #[test]
    fn dns_answers_a_with_fake_ip_and_nothing_else() {
        use simple_dns::{Name, Packet, Question, CLASS, QCLASS, QTYPE, TYPE};
        let mut q = Packet::new_query(7);
        q.questions.push(Question::new(Name::new_unchecked("api.github.com"), QTYPE::TYPE(TYPE::A), QCLASS::CLASS(CLASS::IN), false));
        q.questions.push(Question::new(Name::new_unchecked("api.github.com"), QTYPE::TYPE(TYPE::AAAA), QCLASS::CLASS(CLASS::IN), false));
        let bytes = q.build_bytes_vec().unwrap();
        let mut names = Names::new(Ipv4Address::new(10, 212, 0, 0));
        let answer = dns_answer(&bytes, &mut names).unwrap();
        let reply = Packet::parse(&answer).unwrap();
        assert_eq!(reply.id(), 7);
        assert_eq!(reply.answers.len(), 1);
        assert_eq!(names.name_of(Ipv4Address::new(10, 212, 0, 1)), Some("api.github.com"));
    }

    #[test]
    fn syn_detection() {
        // A SYN from 10.211.0.2:40000 to 10.212.0.1:443.
        let mut buf = vec![0u8; 14 + 20 + 20];
        let mut eth = EthernetFrame::new_unchecked(&mut buf);
        eth.set_ethertype(EthernetProtocol::Ipv4);
        let mut ip = Ipv4Packet::new_unchecked(eth.payload_mut());
        ip.set_version(4);
        ip.set_header_len(20);
        ip.set_total_len(40);
        ip.set_next_header(IpProtocol::Tcp);
        ip.set_src_addr(Ipv4Address::new(10, 211, 0, 2));
        ip.set_dst_addr(Ipv4Address::new(10, 212, 0, 1));
        ip.set_hop_limit(64);
        ip.fill_checksum();
        let mut tcp = TcpPacket::new_unchecked(ip.payload_mut());
        tcp.set_src_port(40000);
        tcp.set_dst_port(443);
        tcp.set_header_len(20);
        tcp.set_syn(true);
        assert_eq!(
            syn_tuple(&buf),
            Some((Ipv4Address::new(10, 211, 0, 2), 40000, Ipv4Address::new(10, 212, 0, 1), 443))
        );
    }
}
