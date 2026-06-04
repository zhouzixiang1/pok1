"""智能测试客户端：能正确处理完整的德州扑克对弈流程。

用法: python test_client.py <服务器IP> <端口> <玩家名称>
示例: python test_client.py 127.0.0.1 10001 BotA
"""
import socket
import sys
import re


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

    print(f"Connected to {host}:{port} as '{name}'", flush=True)

    is_small_blind = False
    stage = None
    my_action_count = 0

    def recv():
        nonlocal buf
        while "\n" not in buf:
            data = sock.recv(4096)
            if not data:
                return None
            buf += data.decode("utf-8")
        line, buf = buf.split("\n", 1)
        return line.strip()

    def send(msg):
        nonlocal my_action_count
        my_action_count += 1
        print(f"  >> {msg}", flush=True)
        sock.sendall((msg + "\n").encode("utf-8"))

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
            print(f"   [Cards: {cards}, Blind: {blind}]", flush=True)
            if is_small_blind:
                send("call")
            continue

        if msg.startswith("flop|"):
            cards = parse_cards(msg.split("|")[1])
            stage = "flop"
            my_action_count = 0
            print(f"   [Flop: {cards}]", flush=True)
            if not is_small_blind:
                send("check")
            continue

        if msg.startswith("turn|"):
            cards = parse_cards(msg.split("|")[1])
            stage = "turn"
            my_action_count = 0
            print(f"   [Turn: {cards}]", flush=True)
            if not is_small_blind:
                send("check")
            continue

        if msg.startswith("river|"):
            cards = parse_cards(msg.split("|")[1])
            stage = "river"
            my_action_count = 0
            print(f"   [River: {cards}]", flush=True)
            if not is_small_blind:
                send("check")
            continue

        # --- 结算 ---
        if msg.startswith("earnChips"):
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
                send("check")
            continue

        if msg == "allin":
            print(f"   [Opponent all-in]", flush=True)
            send("call")
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
