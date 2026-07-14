"""本地诊断客户端：按国赛无分隔符 raw-TCP 流处理完整对局。

它不签发正式证书，也不把本地结果升级为官方合规证据。

用法: python test_client.py <服务器IP> <端口> <玩家名称>
示例: python test_client.py 127.0.0.1 10001 BotA
"""
import socket
import sys
import re

from server.protocol import split_server_messages


SUIT_NAMES = {0: "S", 1: "H", 2: "D", 3: "C"}
RANK_NAMES = {
    0: "2", 1: "3", 2: "4", 3: "5", 4: "6", 5: "7", 6: "8",
    7: "9", 8: "T", 9: "J", 10: "Q", 11: "K", 12: "A",
}


def card_display(s):
    """'<suit,rank>' → 'SA' 'H10' 等"""
    s = s.strip().strip("<>")
    suit, rank = s.split(",")
    return f"{SUIT_NAMES[int(suit)]}{RANK_NAMES[int(rank)]}"


def parse_cards(msg):
    parts = re.findall(r"<\d+,\d+>", msg)
    return " ".join(card_display(p) for p in parts)


def run_client(host, port, name):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((host, port))
    sock.settimeout(120)
    buf = ""
    pending_messages = []

    print(f"Connected to {host}:{port} as '{name}'", flush=True)

    is_small_blind = False
    stage = None
    my_action_count = 0
    in_allin_runout = False

    def recv():
        nonlocal buf, pending_messages
        while not pending_messages:
            messages, buf = split_server_messages(
                buf,
                flush_boundary=False,
            )
            pending_messages.extend(messages)
            if pending_messages:
                break
            # Numeric messages have no lexical terminator. Resolve only after
            # a short idle interval; all structural prefixes keep the normal
            # match timeout so arbitrary TCP fragmentation is preserved.
            sock.settimeout(0.025 if re.fullmatch(
                r"(?:raise [0-9]+|earnChips -?[0-9]+)",
                buf,
            ) else 120)
            try:
                data = sock.recv(4096)
            except socket.timeout:
                messages, buf = split_server_messages(
                    buf,
                    flush_boundary=True,
                )
                pending_messages.extend(messages)
                if not pending_messages:
                    return None
                break
            if not data:
                messages, buf = split_server_messages(
                    buf,
                    flush_boundary=True,
                )
                pending_messages.extend(messages)
                if not pending_messages:
                    return None
                break
            buf += data.decode("ascii")
        return pending_messages.pop(0)

    def send(msg):
        nonlocal my_action_count
        my_action_count += 1
        print(f"  >> {msg}", flush=True)
        sock.sendall(msg.encode("ascii"))

    while True:
        msg = recv()
        if msg is None:
            print("Server disconnected.", flush=True)
            break

        print(f"<< {msg}", flush=True)

        # --- Name query ---
        if msg == "name":
            send(name)
            continue

        # --- 新阶段开始 ---
        if msg.startswith("preflop|"):
            parts = msg.split("|")
            blind = parts[1]
            cards = parse_cards(parts[2])
            is_small_blind = (blind == "SMALLBLIND")
            stage = "preflop"
            my_action_count = 0
            in_allin_runout = False
            print(f"   [Cards: {cards}, Blind: {blind}]", flush=True)
            if is_small_blind:
                send("call")
            continue

        if msg.startswith("flop|"):
            cards = parse_cards(msg.split("|")[1])
            stage = "flop"
            my_action_count = 0
            print(f"   [Flop: {cards}]", flush=True)
            if not is_small_blind and not in_allin_runout:
                send("check")
            continue

        if msg.startswith("turn|"):
            cards = parse_cards(msg.split("|")[1])
            stage = "turn"
            my_action_count = 0
            print(f"   [Turn: {cards}]", flush=True)
            if not is_small_blind and not in_allin_runout:
                send("check")
            continue

        if msg.startswith("river|"):
            cards = parse_cards(msg.split("|")[1])
            stage = "river"
            my_action_count = 0
            print(f"   [River: {cards}]", flush=True)
            if not is_small_blind and not in_allin_runout:
                send("check")
            continue

        # --- 结算 ---
        if msg.startswith("earnChips"):
            in_allin_runout = False
            print(f"   [Earned: {int(msg.split()[1])}]", flush=True)
            continue

        if msg.startswith("oppo_hands|"):
            print(f"   [Opponent: {parse_cards(msg.split('|')[1])}]", flush=True)
            continue

        # --- 对手行为 → 需要响应 ---

        if msg == "call":
            print(f"   [Opponent calls]", flush=True)
            if stage == "preflop" and my_action_count == 0:
                send("check")
            continue

        if msg == "fold":
            print(f"   [Opponent folds]", flush=True)
            continue

        if msg == "check":
            print(f"   [Opponent checks]", flush=True)
            if stage != "preflop":
                send("call")
            continue

        if msg == "allin":
            print(f"   [Opponent all-in]", flush=True)
            send("call")
            in_allin_runout = True
            continue

        if msg.startswith("raise "):
            amount = int(msg.split()[1])
            print(f"   [Opponent raises to {amount}]", flush=True)
            send("call")
            continue

        print(f"   [Unknown message: {msg}]", flush=True)


if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2] if len(sys.argv) > 2 else "10001")
    name = sys.argv[3] if len(sys.argv) > 3 else "TestBot"
    run_client(host, port, name)
