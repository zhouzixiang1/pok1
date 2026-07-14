import socket
import random
import time
import select

# 全局变量定义
NumberOfMatches = 70  # 对局总数
AllCards = [[0, 0] for _ in range(9)]  # 场上所有牌
HandCards = [[0, 0] for _ in range(2)]  # 手牌
FieldCards = [[0, 0] for _ in range(5)]  # 公共牌
KeepCards = [[0, 0] for _ in range(7)]  # 公共牌加任一一方手牌的组合
BlindNote = 0  # 大小盲注标志，0表示小盲注，1表示大盲注
Stage = 0  # 阶段数
Increase = 0  # raise增加的筹码量
SUMbj = 0  # 剩余赌注
SUMbh = 0  # 加注量
bujie = 0
duiju = 0
get = 0
count2 = 0
sendBuf = ""
recvBuf = ""
opBuf = ""
PAIXING = 0  # 牌型
PAIDIAN = 0  # 牌点

# 记录小于等于J的牌
def observe(Colour, Size, Round, Number):
    global HandCards, FieldCards
    
    if Round == 1:
        # 轮次1表示盲注轮，记录两张手牌
        if Number == 1:
            HandCards[0][0] = ord(Colour) - 48
            HandCards[0][1] = ord(Size) - 48
        if Number == 2:
            HandCards[1][0] = ord(Colour) - 48
            HandCards[1][1] = ord(Size) - 48
    elif Round == 2:
        # 轮次2表示公布的三张公共牌
        if Number == 1:
            FieldCards[0][0] = ord(Colour) - 48
            FieldCards[0][1] = ord(Size) - 48
        if Number == 2:
            FieldCards[1][0] = ord(Colour) - 48
            FieldCards[1][1] = ord(Size) - 48
        if Number == 3:
            FieldCards[2][0] = ord(Colour) - 48
            FieldCards[2][1] = ord(Size) - 48
    elif Round == 3:
        # 轮次3表示新公共牌
        FieldCards[3][0] = ord(Colour) - 48
        FieldCards[3][1] = ord(Size) - 48
    elif Round == 4:
        # 轮次4表示新公共牌
        FieldCards[4][0] = ord(Colour) - 48
        FieldCards[4][1] = ord(Size) - 48
    elif Round == 5:
        pass

# 记录大于J的牌
def observes(Colour, Size1, Size2, Round, Number):
    global HandCards, FieldCards
    
    if Round == 1:
        if Number == 1:
            HandCards[0][0] = ord(Colour) - 48
            HandCards[0][1] = 10 * (ord(Size1) - 48) + ord(Size2) - 48
        if Number == 2:
            HandCards[1][0] = ord(Colour) - 48
            HandCards[1][1] = 10 * (ord(Size1) - 48) + ord(Size2) - 48
    elif Round == 2:
        if Number == 1:
            FieldCards[0][0] = ord(Colour) - 48
            FieldCards[0][1] = 10 * (ord(Size1) - 48) + ord(Size2) - 48
        if Number == 2:
            FieldCards[1][0] = ord(Colour) - 48
            FieldCards[1][1] = 10 * (ord(Size1) - 48) + ord(Size2) - 48
        if Number == 3:
            FieldCards[2][0] = ord(Colour) - 48
            FieldCards[2][1] = 10 * (ord(Size1) - 48) + ord(Size2) - 48
    elif Round == 3:
        FieldCards[3][0] = ord(Colour) - 48
        FieldCards[3][1] = 10 * (ord(Size1) - 48) + ord(Size2) - 48
    elif Round == 4:
        FieldCards[4][0] = ord(Colour) - 48
        FieldCards[4][1] = 10 * (ord(Size1) - 48) + ord(Size2) - 48
    elif Round == 5:
        pass

# 计算加注金额
def jsjz():
    global Increase, recvBuf
    
    if len(recvBuf) == 11:
        Increase = (ord(recvBuf[6]) - 48) * 10000 + (ord(recvBuf[7]) - 48) * 1000 + (ord(recvBuf[8]) - 48) * 100 + (ord(recvBuf[9]) - 48) * 10 + (ord(recvBuf[10]) - 48)
    elif len(recvBuf) == 10:
        Increase = (ord(recvBuf[6]) - 48) * 1000 + (ord(recvBuf[7]) - 48) * 100 + (ord(recvBuf[8]) - 48) * 10 + (ord(recvBuf[9]) - 48)
    elif len(recvBuf) == 9:
        Increase = (ord(recvBuf[6]) - 48) * 100 + (ord(recvBuf[7]) - 48) * 10 + (ord(recvBuf[8]) - 48)
    elif len(recvBuf) == 8:
        Increase = (ord(recvBuf[6]) - 48) * 10 + (ord(recvBuf[7]) - 48)
    elif len(recvBuf) == 7:
        Increase = (ord(recvBuf[6]) - 48)

# 判定胜负
def pdsy():
    global AllCards, KeepCards, PAIXING, PAIDIAN
    
    ap = [[0, 0] for _ in range(7)]
    bp = [[0, 0] for _ in range(7)]
    
    # 复制数据
    for i in range(7):
        for j in range(2):
            ap[i][j] = AllCards[i][j]
    
    for i in range(2, 9):
        for j in range(2):
            bp[i-2][j] = AllCards[i][j]
    
    # 牌值大小从大到小冒泡排序
    for i in range(1, 7):
        for j in range(i, 7):
            if ap[i-1][1] <= ap[j][1]:
                ap[i-1][1], ap[j][1] = ap[j][1], ap[i-1][1]
                ap[i-1][0], ap[j][0] = ap[j][0], ap[i-1][0]
            if bp[i-1][1] <= bp[j][1]:
                bp[i-1][1], bp[j][1] = bp[j][1], bp[i-1][1]
                bp[i-1][0], bp[j][0] = bp[j][0], bp[i-1][0]
    
    # keepcards赋值为排序好的ap数组
    for i in range(7):
        for j in range(2):
            KeepCards[i][j] = ap[i][j]
    
    zuida()
    a1 = PAIXING
    a2 = PAIDIAN
    
    for i in range(7):
        for j in range(2):
            KeepCards[i][j] = bp[i][j]
    
    zuida()
    b1 = PAIXING
    b2 = PAIDIAN
    
    # 比较大小
    if a1 > b1:
        return 1
    elif a1 == b1:
        if a2 > b2:
            return 1
        elif a2 == b2:
            return 0
        else:
            return -1
    else:
        return -1

# 牌型大小检测
def zuida():
    global KeepCards, PAIXING, PAIDIAN
    
    # 牌型为高牌，点数为最大牌
    PAIXING = 0
    PAIDIAN = KeepCards[0][1]
    
    # 检测对子
    for i in range(6):
        for j in range(i+1, 7):
            if KeepCards[i][1] == KeepCards[j][1]:
                PAIXING = 1
                PAIDIAN = KeepCards[i][1]
                break
        if PAIXING == 1:
            break
    
    c = 0
    # 检测两对
    for i in range(6):
        if c == 0:
            for j in range(i+1, 7):
                if KeepCards[i][1] == KeepCards[j][1]:
                    i += 1
                    c = 1
                    break
        else:
            for j in range(i+1, 7):
                if KeepCards[i][1] == KeepCards[j][1]:
                    PAIXING = 2
                    break
        if PAIXING == 2:
            break
    
    # 检测三条
    for i in range(5):
        for j in range(i+1, 6):
            if ((KeepCards[i][1] == KeepCards[j][1]) and 
                (KeepCards[i][1] == KeepCards[j+1][1])):
                PAIXING = 3
                PAIDIAN = KeepCards[i][1]
                break
        if PAIXING == 3:
            break
    
    # 检测顺子
    for i in range(3):
        d = 0
        c = KeepCards[i][1]
        for j in range(i+1, 7):
            if c == (KeepCards[j][1] + 1):
                c -= 1
                d += 1
        if d > 3:
            PAIXING = 4
            PAIDIAN = KeepCards[i][1]
            break
    
    # 检测同花
    for i in range(3):
        c = 0
        for j in range(i+1, 7):
            if KeepCards[i][0] == KeepCards[j][0]:
                c += 1
        if c > 3:
            PAIXING = 5
            PAIDIAN = KeepCards[i][1]
            break
    
    # 检测三带二(葫芦)
    c = 0
    for i in range(5):
        if c == 0:
            if (i+2 < 7 and (KeepCards[i][1] == KeepCards[i+1][1]) and 
                (KeepCards[i][1] == KeepCards[i+2][1])):
                c = 1
                PAIDIAN = KeepCards[i][1]
                i = 0
        else:
            for j in range(6):
                if (j+1 < 7 and (KeepCards[j][1] == KeepCards[j+1][1]) and 
                    (KeepCards[j][1] != PAIDIAN)):
                    PAIXING = 6
                    break
        if PAIXING == 6:
            break
    
    # 检测四条
    for i in range(4):
        if (i+3 < 7 and (KeepCards[i][1] == KeepCards[i+1][1]) and 
            (KeepCards[i][1] == KeepCards[i+2][1]) and 
            (KeepCards[i][1] == KeepCards[i+3][1])):
            PAIDIAN = KeepCards[i][1]
            PAIXING = 7
            break
    
    # 检测同花顺
    for i in range(3):
        d = 0
        c = KeepCards[i][1]
        e = KeepCards[i][0]
        for j in range(i+1, 7):
            if ((c == (KeepCards[j][1] + 1)) and (e == KeepCards[j][0])):
                c -= 1
                d += 1
        if d > 3:
            PAIXING = 8
            PAIDIAN = KeepCards[i][1]
            break

# 随机分配
def sjfp(stage):
    global AllCards, HandCards, FieldCards
    
    AllCards[0][0] = HandCards[0][0]
    AllCards[0][1] = HandCards[0][1]
    AllCards[1][0] = HandCards[1][0]
    AllCards[1][1] = HandCards[1][1]
    
    # 阶段1已知手牌，随机生成公共牌和对手手牌并避免重复
    if stage == 1:
        for i in range(2, 9):
            c = 0
            while c == 0:
                a = random.randint(0, 3)
                b = random.randint(0, 12)
                c = 1
                for j in range(i):
                    if (a == AllCards[j][0]) and (b == AllCards[j][1]):
                        c = 0
                        break
            AllCards[i][0] = a
            AllCards[i][1] = b
    
    # 阶段2已知手牌和三张公共牌，随机生成剩余公共牌和对手手牌并避免重复
    elif stage == 2:
        for i in range(3):
            AllCards[i+2][0] = FieldCards[i][0]
            AllCards[i+2][1] = FieldCards[i][1]
        
        for i in range(5, 9):
            c = 0
            while c == 0:
                a = random.randint(0, 3)
                b = random.randint(0, 12)
                c = 1
                for j in range(i):
                    if (a == AllCards[j][0]) and (b == AllCards[j][1]):
                        c = 0
                        break
            AllCards[i][0] = a
            AllCards[i][1] = b
    
    # 阶段3已知手牌和四张公共牌，随机生成剩余公共牌和对手手牌并避免重复
    elif stage == 3:
        for i in range(4):
            AllCards[i+2][0] = FieldCards[i][0]
            AllCards[i+2][1] = FieldCards[i][1]
        
        for i in range(6, 9):
            c = 0
            while c == 0:
                a = random.randint(0, 3)
                b = random.randint(0, 12)
                c = 1
                for j in range(i):
                    if (a == AllCards[j][0]) and (b == AllCards[j][1]):
                        c = 0
                        break
            AllCards[i][0] = a
            AllCards[i][1] = b
    
    # 阶段4已知手牌和五张公共牌，随机生成对手手牌并避免重复
    elif stage == 4:
        for i in range(5):
            AllCards[i+2][0] = FieldCards[i][0]
            AllCards[i+2][1] = FieldCards[i][1]
        
        for i in range(7, 9):
            c = 0
            while c == 0:
                a = random.randint(0, 3)
                b = random.randint(0, 12)
                c = 1
                for j in range(i):
                    if (a == AllCards[j][0]) and (b == AllCards[j][1]):
                        c = 0
                        break
            AllCards[i][0] = a
            AllCards[i][1] = b

# 获胜概率计算
def shenglishu(nnn):  # nnn为阶段数
    shengli = 0
    random.seed(time.time())
    
    for i in range(200000):
        sjfp(nnn)
        nou = pdsy()
        shengli += nou
    
    return shengli

# 随机概率生成
def prob(a, b):
    random.seed(time.time())
    return random.randint(a, b)

# 延迟函数
def delay():
    time.sleep(0.3)  # 简化延迟函数，原C代码中是一个多层循环延迟

# 亮牌底的对手手牌处理
def oppo():
    global bujie, opBuf, recvBuf, sendBuf
    
    n = 0
    m = 0
    opBuf = ""
    
    for i in range(100):
        if i < len(recvBuf):
            if n < 2:
                if recvBuf[i] == '>':
                    n += 1
            else:
                opBuf += recvBuf[i]
                m += 1
    
    recvBuf = opBuf
    bujie = 2
    sendBuf = ""

# preflop阶段策略
def pref():
    global BlindNote, SUMbh, SUMbj, sendBuf, bujie, opBuf, recvBuf, duiju, get
    
    n = 0
    m = 0
    
    # 计算剩余局数
    remaining_matches = NumberOfMatches - duiju
    
    # 根据剩余局数判断是否直接fold
    if remaining_matches % 2 == 0:  # 剩余局数为偶数
        if get > (remaining_matches // 2) * 150:
            sendBuf = "fold"
            return
    else:  # 剩余局数为奇数
        if get > ((remaining_matches - 1) // 2) * 150 + 100:
            sendBuf = "fold"
            return

    # 大小盲注判断
    print(get)
    if recvBuf[8] == 'B':
        sendBuf = ""
        BlindNote = 1
        SUMbh = 100
        
        if len(recvBuf) > 30:
            opBuf = ""
            bujie = 2
            for i in range(100):
                if i < len(recvBuf):
                    if n < 2:
                        if recvBuf[i] == '>':
                            n += 1
                    else:
                        opBuf += recvBuf[i]
                        m += 1
            
            recvBuf = opBuf
        
    
    # 小盲注表态
    else:
        BlindNote = 0
        
        random.seed(time.time())
        sl = shenglishu(1)
        
        # 胜利数判断是否加码还是跟注
        if sl > 4000:
            sendBuf = "raise 600"
            SUMbh = 600
        elif sl > 2000:
            sendBuf = "raise 300"
            SUMbh = 300
        else:
            sendBuf = "call"
            SUMbh = 100

# flop阶段策略
def flop():
    global BlindNote, SUMbj, SUMbh, sendBuf
    
    # 大盲注表态
    if BlindNote == 1:
        sendBuf = "check"
        return
    else:
        sendBuf = ""

# turn阶段策略
def turn():
    global BlindNote, SUMbj, SUMbh, sendBuf
    
    # 大盲注表态
    if BlindNote == 1:
        random.seed(time.time())
        sl = shenglishu(3)
        
        if sl > 8000:
            sendBuf = "allin"
        elif sl > 4000:
            if SUMbj > 5000:
                SUMbh = 600
                sendBuf = zhenghe(600)
            else:
                sendBuf = "check"
        elif sl > 2000:
            if SUMbj > 10000:
                SUMbh = 300
                sendBuf = zhenghe(300)
            else:
                sendBuf = "check"
        else:
            sendBuf = "check"
    else:
        sendBuf = ""

# river阶段策略
def rive():
    global BlindNote, SUMbj, SUMbh, sendBuf
    
    # 大盲注表态
    if BlindNote == 1:
        random.seed(time.time())
        sl = shenglishu(4)
        
        if sl > 8000:
            sendBuf = "allin"
        elif sl > 2000:
            if SUMbj > 10000:
                SUMbh = 600
                sendBuf = zhenghe(600)
            else:
                sendBuf = "check"
        else:
            sendBuf = "check"
    else:
        sendBuf = ""

# 对对手check行为表态
def chec():
    global SUMbj, SUMbh, Stage, sendBuf
    
    random.seed(time.time())
    sl = shenglishu(Stage)
    
    if sl > 8000:
        sendBuf = "allin"
    elif sl > 4000:
        if SUMbj > 10000:
            SUMbh = 600
            sendBuf = zhenghe(600)
        else:
            sendBuf = "call"
    elif sl > 2000:
        if SUMbj > 18000:
            SUMbh = 300
            sendBuf = zhenghe(300)
        else:
            sendBuf = "call"
    else:
        sendBuf = "call"

# 对对手call行为表态
def call():
    global SUMbj, SUMbh, sendBuf
    
    random.seed(time.time())
    sl = shenglishu(1)
    
    if sl > 4000:
        if SUMbj > 2000:
            SUMbh = 600
            sendBuf = "raise 600"
        else:
            sendBuf = "allin"
    elif sl > 2000:
        if SUMbj > 2000:
            SUMbh = 300
            sendBuf = "raise 300"
        else:
            sendBuf = "allin"
    else:
        sendBuf = "fold"

# 对对手raise行为表态
def rais():
    global SUMbj, SUMbh, Increase, Stage, sendBuf
    
    random.seed(time.time())
    sl = shenglishu(Stage)
    
    # 计算最小加注金额
    min_raise = 0
    if Increase > 0:  # 如果前面有玩家下注
        min_raise = 2 * Increase  # 最小加注为当前下注的2倍
    else:  # 如果前面没有玩家下注
        min_raise = 100  # 最小加注为大盲注
    
    if sl > 8000:  # 非常好的牌
        if SUMbj >= min_raise:  # 如果剩余筹码足够最小加注
            SUMbh = max(min_raise, 800)
            sendBuf = zhenghe(min_raise)
        else:
            sendBuf = "allin"
    elif sl > 7000:  # 很好的牌
        if Increase > 9999:  # 对手加注太大
            SUMbh = Increase
            sendBuf = "call"
        elif SUMbj >= min_raise:  # 可以加注
            SUMbh = max(min_raise, 600)
            sendBuf = zhenghe(min_raise)
        else:
            sendBuf = "allin"
    elif sl > 4000:  # 不错的牌
        if Increase > 8000:  # 对手加注太大
            sendBuf = "fold"
        elif Increase > 2000:  # 对手加注较大
            SUMbh = Increase
            sendBuf = "call"
        elif SUMbj >= min_raise:  # 可以加注
            SUMbh = max(min_raise, 400)
            sendBuf = zhenghe(min_raise)
        else:
            sendBuf = "allin"
    elif sl > 2000:  # 一般的牌
        if Increase > 5000:  # 对手加注太大
            sendBuf = "fold"
        elif Increase > 1000:  # 对手加注较大
            SUMbh = Increase
            sendBuf = "call"
        elif SUMbj >= min_raise:  # 可以加注
            SUMbh = max(min_raise, 200)
            sendBuf = zhenghe(min_raise)
        else:
            sendBuf = "allin"
    elif sl > 0:  # 较差的牌
        if Increase > 3000:  # 对手加注太大
            sendBuf = "fold"
        else:
            SUMbh = Increase
            sendBuf = "call"
    else:  # 很差的牌
        if Increase > 1000:  # 对手加注太大
            sendBuf = "fold"
        else:
            SUMbh = Increase
            sendBuf = "call"

# 对对手allin行为表态
def alli():
    global Stage, sendBuf,get,duiju,NumberOfMatches,SUMbj
    
    random.seed(time.time())
    sl = shenglishu(Stage)
    
    # 计算剩余局数
    remaining_matches = NumberOfMatches - duiju
    
    # 根据剩余局数判断是否直接fold
    if remaining_matches % 2 == 0:  # 剩余局数为偶数
        bisheng=remaining_matches // 2 * 150
    else: 
        bisheng=(remaining_matches - 1) // 2 * 150 + 100

    if sl > 8000 or 20000-SUMbj-get > bisheng:
        sendBuf = "call"
    elif sl > 6500:
        if prob(1, 5) >= 3:
            sendBuf = "call"
        else:
            sendBuf = "fold"
    elif sl > 5000:
        if prob(1, 5) >= 1:
            sendBuf = "call"
        else:
            sendBuf = "fold"
    else:
        sendBuf = "fold"

# 加注金额格式化
def zhenghe(nnn):
    if nnn >= 10000:
        return f"raise {nnn}"
    elif nnn > 1000:
        return f"raise {nnn}"
    elif nnn > 100:
        return f"raise {nnn}"
    elif nnn > 10:
        return f"raise {nnn}"
    elif nnn > 1:
        return f"raise {nnn}"
    elif nnn == 0:
        return "call"
    return ""

# 主函数
def main():
    global BlindNote, SUMbj, SUMbh, Stage, Increase, duiju, bujie, get, count2, recvBuf, sendBuf
    
    # 创建TCP连接
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_address = ("127.0.0.1", 10001)
    
    try:
        print("1")
        sock.connect(server_address)
        print("load success")
        
        duiju = 0
        SUMbh = 0
        random.seed(time.time())
        get = 0
        
        while duiju <= NumberOfMatches:
            if count2 == 9:
                if BlindNote == 0:
                    sendBuf = "call"
                else:
                    sendBuf = "check"
                count2 = 0
                print(sendBuf)
                sock.send(sendBuf.encode())
                sendBuf = ""
                print("send message successfully")
                continue
            
            if bujie == 0:
                print("try to receive")
                recvBuf = ""
                print("memset successfully")
                
                # 使用select实现非阻塞接收
                try:
                    readfds = [sock]
                    timeout = 5  # 设置超时时间为5秒
                    
                    ready, _, _ = select.select(readfds, [], [], timeout)
                    
                    if not ready:
                        print("接收数据超时，执行下一语句")
                        count2 += 1
                        continue
                    
                    recvResult = sock.recv(100)
                    if recvResult:
                        recvBuf = recvResult.decode()
                        print(f"收到的消息: {recvBuf}")
                    else:
                        print("连接关闭")
                        continue
                except Exception as e:
                    print(f"接收数据错误: {e}")
                    continue
            
            # 轮次判断，更新轮次指标Stage
            for i in range(15):
                if i < len(recvBuf) and recvBuf[i] == '|':
                    if len(recvBuf) > 3:
                        if recvBuf[3] == 'f':
                            Stage = 1  # pref
                        elif recvBuf[3] == 'p':
                            Stage = 2  # flop
                        elif recvBuf[3] == 'n':
                            Stage = 3  # turn
                        elif recvBuf[3] == 'e':
                            Stage = 4  # river
                        elif recvBuf[3] == 'o':
                            Stage = 5  # oppo
                    break
            
            number = 0
            for i in range(30):
                if i < len(recvBuf) and recvBuf[i] == '<':
                    number += 1
                    if i + 4 < len(recvBuf) and recvBuf[i + 4] == '>':
                        observe(recvBuf[i + 1], recvBuf[i + 3], Stage, number)
                    else:
                        observes(recvBuf[i + 1], recvBuf[i + 3], recvBuf[i + 4], Stage, number)
            
            # 根据接收到的消息调用相应的处理函数
            if recvBuf.startswith("name"):
                sendBuf = "底牌码农"
            elif recvBuf.startswith("pref"):
                SUMbh = 0
                SUMbj = 20000
                print(f"剩余赌注 {SUMbj}")
                duiju += 1
                pref()
            elif recvBuf.startswith("flop"):
                SUMbj -= SUMbh
                print(f"剩余赌注 {SUMbj}")
                SUMbh = 0
                flop()
                print("flop successfully")
            elif recvBuf.startswith("turn"):
                SUMbj -= SUMbh
                print(f"剩余赌注 {SUMbj}")
                SUMbh = 0
                turn()
            elif recvBuf.startswith("rive"):
                SUMbj -= SUMbh
                print(f"剩余赌注 {SUMbj}")
                SUMbh = 0
                rive()
            elif recvBuf.startswith("check"):
                chec()
            elif recvBuf.startswith("call"):
                call()
            elif recvBuf.startswith("oppo"):
                if len(recvBuf) < 30:
                    sendBuf = ""
                else:
                    oppo()
            elif recvBuf.startswith("earn"):
                sendBuf = ""
                print(len(recvBuf))
                if recvBuf[10] == '-':
                    ws = 11
                    change = 0
                    while ws < len(recvBuf) and recvBuf[ws] >= '0' and recvBuf[ws] <= '9':
                        change = change * 10 + int(recvBuf[ws])
                        ws += 1
                    print("/n",change)
                    get -= change
                else:
                    ws = 10
                    change = 0
                    while ws < len(recvBuf) and recvBuf[ws] >= '0' and recvBuf[ws] <= '9':
                        change = change * 10 + int(recvBuf[ws])
                        ws += 1
                    print("/n",change)
                    get += change
            elif recvBuf.startswith("alli"):
                alli()
            elif recvBuf.startswith("raise"):
                jsjz()
                rais()
            
            # 延迟
            delay()
            
            # 发送消息
            if sendBuf:
                sock.send(sendBuf.encode())
                print(sendBuf)
                sendBuf = ""
                print("send message successfully")
            
            if bujie > 0:
                bujie -= 1
        
        # 关闭连接
        sock.close()
    
    except Exception as e:
        print(f"发生错误: {e}")
        sock.close()

if __name__ == "__main__":
    main()