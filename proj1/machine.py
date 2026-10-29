"""
machine.py – EMULATOR modułu `machine` (MicroPython / ESP8266) dla komputera PC.
Systemy wbudowane – laboratorium, Politechnika Wrocławska.

!!! Ten plik służy WYŁĄCZNIE do uruchamiania programów na PC. NIE wgrywaj go na ESP8266 !!!

Co emuluje:
  * Pin  – wyjścia (zmiany stanu wypisywane w konsoli z czasem) i wejścia (przycisk z klawiatury
           albo ze scenariusza), PULL_UP, irq(),
  * PWM  – freq(), duty(), duty_u16(), deinit(),
  * Timer – ONE_SHOT / PERIODIC z funkcją callback,
  * dodaje do modułu `time` funkcje MicroPythona: sleep_ms, sleep_us, ticks_ms, ticks_us,
    ticks_diff, ticks_add (wystarczy zaimportować machine przed ich użyciem).

Przycisk z klawiatury (domyślnie pierwszy pin utworzony jako wejście):
  Enter            -> krótkie wciśnięcie (200 ms)
  l + Enter        -> długie wciśnięcie (2500 ms)
  liczba + Enter   -> wciśnięcie na podaną liczbę ms, np. 1200

Funkcje symulatora (nie istnieją na ESP8266 – używaj ich tylko w testach i narzędziach):
  sim_config(fast=..., log=..., names=..., keyboard_pin=...)
  sim_button(gpio, [(start_ms, czas_ms), ...])   – scenariusz wciśnięć (czasy względem "teraz")
  sim_time()                                     – bieżący czas symulacji [ms]
  sim_history(gpio=None)                         – lista zmian stanu (t_ms, gpio, wartość)
  sim_intervals(gpio)                            – przedziały (t_start, t_koniec), gdy wyjście było AKTYWNE
  sim_ascii(gpios, step_ms=100)                  – przebieg czasowy w konsoli
  sim_plot("plik.png", gpios)                    – przebieg czasowy jako obraz (wymaga matplotlib)
"""
import sys
import time as _time
import builtins
import threading
import queue

try:                                   # konsola Windows: nie przerywaj programu z powodu znaków ●/○
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

TICKS_PERIOD = 1 << 30
_HALF = TICKS_PERIOD // 2
_orig_sleep = _time.sleep
_orig_input = builtins.input


class _S:
    fast = False
    log = True
    vt = 0.0
    t0 = _time.perf_counter()
    # gpio -> (nazwa, aktywny_stanem_niskim)
    names = {2: ("LED", True), 16: ("LED2", True)}
    history = []
    pins = {}
    scripts = {}
    kb_pin = None
    q = None
    press_until = {}
    timers = []
    in_service = False
    warned = set()


# ------------------------------------------------------------------ zegar
def _now():
    if _S.fast:
        return _S.vt
    return (_time.perf_counter() - _S.t0) * 1000.0


def _ts():
    return "[{:9.3f} s]".format(_now() / 1000.0)


def _out(text):
    if _S.log:
        print(text)


def _next_timer_due(default):
    due = [tm._next for tm in _S.timers if tm._active]
    return min(due + [default])


def ticks_ms():
    if _S.fast:
        _S.vt += 0.001          # ochrona przed nieskończoną pętlą bez sleep
    _service()
    return int(_now()) % TICKS_PERIOD


def ticks_us():
    if _S.fast:
        _S.vt += 0.001
    _service()
    return int(_now() * 1000) % TICKS_PERIOD


def ticks_diff(a, b):
    return ((a - b + _HALF) % TICKS_PERIOD) - _HALF


def ticks_add(t, d):
    return (t + d) % TICKS_PERIOD


def sleep_ms(ms):
    end = _now() + ms
    if _S.fast:
        while True:
            _S.vt = max(_S.vt, min(_next_timer_due(end), end))
            _service()
            if _S.vt >= end:
                break
    else:
        while True:
            rem = end - _now()
            if rem <= 0:
                break
            step = min(rem, max(0.0, _next_timer_due(end) - _now()), 20.0)
            _orig_sleep(step / 1000.0)
            _service()


def sleep_us(us):
    sleep_ms(us / 1000.0)


def sleep(s):
    sleep_ms(s * 1000.0)


for _n in ("ticks_ms", "ticks_us", "ticks_diff", "ticks_add", "sleep_ms", "sleep_us", "sleep"):
    setattr(_time, _n, globals()[_n])


# ------------------------------------------------------------------ klawiatura / input()
def _ensure_reader():
    if _S.q is not None:
        return
    _S.q = queue.Queue()

    def reader():
        while True:
            line = sys.stdin.readline()
            if not line:
                _S.q.put(None)
                break
            _S.q.put(line.rstrip("\r\n"))
    threading.Thread(target=reader, daemon=True).start()


def _input(prompt=""):
    if _S.q is None:
        line = _orig_input(prompt)
        if not sys.stdin.isatty():
            print(line)
        return line
    sys.stdout.write(str(prompt))
    sys.stdout.flush()
    while True:
        try:
            line = _S.q.get(timeout=0.05)
            break
        except queue.Empty:
            continue
    if line is None:
        _S.q.put(None)
        raise EOFError
    if not sys.stdin.isatty():
        print(line)
    return line


builtins.input = _input


# ------------------------------------------------------------------ obsługa zdarzeń
def _service():
    if _S.in_service:
        return
    _S.in_service = True
    try:
        for tm in list(_S.timers):
            while tm._active and _now() >= tm._next:
                if tm._mode == Timer.ONE_SHOT:
                    tm._active = False
                else:
                    tm._next += tm._period
                if tm._cb:
                    tm._cb(tm)
        for p in list(_S.pins.values()):
            if p._mode == Pin.IN and p._handler:
                p._update(p._read_input())
    finally:
        _S.in_service = False


def _desc(gpio):
    name, _ = _S.names.get(gpio, (None, False))
    return "{} (GPIO{})".format(name, gpio) if name else "GPIO{}".format(gpio)


# ------------------------------------------------------------------ Pin
class Pin:
    IN = 0
    OUT = 1
    OPEN_DRAIN = 2
    PULL_UP = 1
    IRQ_RISING = 1
    IRQ_FALLING = 2

    def __new__(cls, id, *args, **kwargs):
        # jeden obiekt na numer GPIO – tak jak jeden fizyczny pin
        if id in _S.pins:
            return _S.pins[id]
        return super().__new__(cls)

    def __init__(self, id, mode=-1, pull=-1, value=None):
        if getattr(self, "_id", None) == id:     # pin już istnieje – tylko ewentualna rekonfiguracja
            if mode != -1 or pull != -1 or value is not None:
                self.init(mode, pull, value)
            return
        self._id = id
        self._v = None
        self._mode = None          # None = pin nieskonfigurowany (np. Pin(2) przekazany do PWM)
        self._pull = -1
        self._handler = None
        self._trigger = 0
        self._last = None
        self.init(mode, pull, value)

    def init(self, mode=-1, pull=-1, value=None):
        if mode != -1:
            self._mode = mode
        if pull != -1:
            self._pull = pull
        _S.pins[self._id] = self
        if self._mode is None:
            return
        if self._mode in (Pin.OUT, Pin.OPEN_DRAIN):
            v = self._v if value is None else (1 if value else 0)
            self._set(0 if v is None else v)
        else:
            if self._pull != Pin.PULL_UP and self._id not in _S.warned:
                _S.warned.add(self._id)
                print("[SIM] Uwaga: GPIO{} jest wejściem bez Pin.PULL_UP – na prawdziwej płytce "
                      "niepodłączone wejście 'pływa' (losowe 0/1).".format(self._id))
            if self._id not in _S.names:
                _S.names[self._id] = ("Przycisk", self._pull == Pin.PULL_UP)
            if _S.kb_pin is None and self._id not in _S.scripts:
                _S.kb_pin = self._id
                _out("[SIM] Przycisk GPIO{} sterowany klawiaturą: Enter = krótkie wciśnięcie (0,2 s), "
                      "l+Enter = długie (2,5 s), liczba+Enter = wciśnięcie na tyle ms.".format(self._id))
            if self._last is None:
                self._last = 1 if self._pull == Pin.PULL_UP else 0
                _S.history.append((_now(), self._id, self._last))

    # --- wejście
    def _read_input(self):
        t = _now()
        pressed = False
        if self._id in _S.scripts:
            pressed = any(a <= t < b for a, b in _S.scripts[self._id])
        elif self._id == _S.kb_pin:
            end = _S.press_until.get(self._id, -1e9)
            if t < end:
                pressed = True
            elif t < end + 100:        # min. 100 ms przerwy między kolejnymi wciśnięciami z kolejki
                pressed = False
            else:
                _ensure_reader()
                try:
                    line = _S.q.get_nowait()
                except queue.Empty:
                    line = False
                if line is None:
                    _S.q.put(None)
                elif line is not False:
                    s = line.strip().lower()
                    dur = int(s) if s.isdigit() else (2500 if s == "l" else 200)
                    _S.press_until[self._id] = t + dur
                    pressed = True
        if pressed:
            return 0
        return 1 if self._pull == Pin.PULL_UP else 0

    def _update(self, v):
        if v == self._last:
            return
        old, self._last = self._last, v
        if self._id not in _S.scripts:        # dla scenariusza historia jest zapisana w sim_button()
            _S.history.append((_now(), self._id, v))
        _out("{} {}: {}".format(_ts(), _desc(self._id), "WCIŚNIĘTY (0)" if v == 0 else "puszczony (1)"))
        if self._handler:
            if (old == 1 and v == 0 and self._trigger & Pin.IRQ_FALLING) or \
               (old == 0 and v == 1 and self._trigger & Pin.IRQ_RISING):
                self._handler(self)

    # --- wyjście
    def _set(self, v):
        v = 1 if v else 0
        if v == self._v:
            return
        self._v = v
        _S.history.append((_now(), self._id, v))
        name, active_low = _S.names.get(self._id, (None, False))
        if name:
            on = (v == 0) if active_low else (v == 1)
            _out("{} {} = {}   {}".format(_ts(), _desc(self._id), v, "● ŚWIECI" if on else "○ zgaszona"))
        else:
            _out("{} {} = {}".format(_ts(), _desc(self._id), v))

    # --- API MicroPythona
    def value(self, x=None):
        if x is None:
            if self._mode == Pin.IN:
                _service()
                v = self._read_input()
                self._update(v)
                return v
            return self._v
        self._set(x)
        _service()

    __call__ = value

    def on(self):
        self.value(1)

    def off(self):
        self.value(0)

    def irq(self, handler=None, trigger=IRQ_FALLING | IRQ_RISING):
        self._handler = handler
        self._trigger = trigger
        return None

    def __repr__(self):
        return "Pin({})".format(self._id)


# ------------------------------------------------------------------ PWM
class PWM:
    def __init__(self, pin, freq=1000, duty=None):
        self._pin = pin if isinstance(pin, Pin) else Pin(pin, Pin.OUT)
        self._f = freq
        self._d = None
        self._pin._mode = Pin.OUT
        _out("{} PWM {}: start, freq = {} Hz".format(_ts(), _desc(self._pin._id), freq))
        self.duty(0 if duty is None else duty)

    def freq(self, f=None):
        if f is None:
            return self._f
        if not 1 <= f <= 1000:
            print("[SIM] Uwaga: na ESP8266 PWM pracuje w zakresie 1–1000 Hz.")
        self._f = f

    def duty(self, d=None):
        if d is None:
            return self._d
        d = max(0, min(1023, int(d)))
        if d == self._d:
            return
        self._d = d
        _S.history.append((_now(), self._pin._id, d / 1023.0))
        name, active_low = _S.names.get(self._pin._id, (None, False))
        jas = (1023 - d) / 1023.0 if active_low else d / 1023.0
        bar = "#" * int(round(jas * 10)) + "." * (10 - int(round(jas * 10)))
        _out("{} PWM {}: duty = {:4d}/1023  jasność [{}] {:3.0f}%".format(_ts(), _desc(self._pin._id), d, bar, jas * 100))
        _service()

    def duty_u16(self, d=None):
        if d is None:
            return int(self._d * 65535 / 1023)
        self.duty(d * 1023 // 65535)

    def deinit(self):
        _out("{} PWM {}: deinit".format(_ts(), _desc(self._pin._id)))


# ------------------------------------------------------------------ Timer
class Timer:
    ONE_SHOT = 0
    PERIODIC = 1

    def __init__(self, id=-1):
        self._active = False
        self._next = 0
        self._period = 0
        self._mode = Timer.PERIODIC
        self._cb = None

    def init(self, mode=PERIODIC, period=-1, freq=-1, callback=None):
        if period == -1 and freq > 0:
            period = 1000.0 / freq
        self._mode, self._period, self._cb = mode, period, callback
        self._next = _now() + period
        self._active = True
        if self not in _S.timers:
            _S.timers.append(self)

    def deinit(self):
        self._active = False


# ------------------------------------------------------------------ różne
def reset():
    print("[SIM] machine.reset() – koniec programu")
    raise SystemExit


def soft_reset():
    reset()


def freq(f=None):
    return 80000000 if f is None else None


def unique_id():
    return b"\x00\x5e\xa1\x8e"


def idle():
    sleep_ms(1)


# ================================================================== funkcje symulatora
def sim_config(fast=None, log=None, names=None, keyboard_pin=None):
    """fast=True: czas wirtualny (sleep nie czeka naprawdę – testy trwają milisekundy).
    log=False: wyłącza wypisywanie zmian stanu. names={12: ("K", False)}: nazwy pinów
    (drugi element: True, gdy element jest aktywny stanem niskim)."""
    if fast is not None and fast != _S.fast:
        now = _now()
        _S.fast = fast
        if fast:
            _S.vt = now
        else:
            _S.t0 = _time.perf_counter() - now / 1000.0
    if log is not None:
        _S.log = log
    if names:
        _S.names.update(names)
    if keyboard_pin is not None:
        _S.kb_pin = keyboard_pin


def sim_button(gpio, presses):
    """Scenariusz wciśnięć przycisku: [(start_ms, czas_trwania_ms), ...] względem chwili wywołania."""
    t = _now()
    _S.scripts[gpio] = [(t + a, t + a + d) for a, d in presses]
    _S.history = [h for h in _S.history if not (h[1] == gpio and h[0] > t)]
    for a, b in _S.scripts[gpio]:
        _S.history.append((a, gpio, 0))
        _S.history.append((b, gpio, 1))
    _S.history.sort(key=lambda h: h[0])
    if _S.kb_pin == gpio:
        _S.kb_pin = None


def sim_time():
    return _now()


def sim_history(gpio=None):
    return [h for h in _S.history if gpio is None or h[1] == gpio]


def sim_intervals(gpio, active=None):
    """Przedziały czasu [ms], w których pin był AKTYWNY (LED świeciła / K załączona).
    active: wartość oznaczająca stan aktywny (domyślnie 0 dla pinów aktywnych stanem niskim, inaczej 1)."""
    if active is None:
        active = 0 if _S.names.get(gpio, (None, False))[1] else 1
    out, start = [], None
    now = _now()
    for t, g, v in sorted(_S.history, key=lambda h: h[0]):
        if t > now:
            break
        if g != gpio or isinstance(v, float):
            continue
        if v == active and start is None:
            start = t
        elif v != active and start is not None:
            out.append((start, t))
            start = None
    if start is not None:
        out.append((start, now))
    return out


def _level_at(gpio, t):
    lvl = None
    for tt, g, v in _S.history:
        if g == gpio and tt <= t <= _now():
            lvl = v
    return lvl


def sim_ascii(gpios, step_ms=100, t_from=0.0, t_to=None):
    """Rysuje przebiegi w konsoli: '#' = stan AKTYWNY (LED świeci, przycisk wciśnięty, K załączona), '_' = nieaktywny.
    Znak '#' pojawia się, jeśli stan aktywny wystąpił w dowolnym momencie danego przedziału."""
    t_to = _now() if t_to is None else t_to
    n = int((t_to - t_from) / step_ms) + 1
    print("Przebieg czasowy: 1 znak = {} ms, od {:.1f} s do {:.1f} s".format(step_ms, t_from / 1000, t_to / 1000))
    for g in gpios:
        name = _S.names.get(g, ("GPIO{}".format(g), False))[0]
        on = sim_intervals(g)
        line = ""
        for i in range(n):
            a, b = t_from + i * step_ms, t_from + (i + 1) * step_ms
            if any(x < b and y > a for x, y in on):
                line += "#"
            else:
                line += "_"
        print("{:>10} |{}".format(name[:10], line))


def sim_plot(filename="przebieg.png", gpios=None, title="Przebieg czasowy (symulacja)"):
    """Zapisuje przebiegi czasowe do pliku PNG (wymaga: pip install matplotlib)."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[SIM] Brak biblioteki matplotlib – zainstaluj: pip install matplotlib")
        return
    gpios = gpios or sorted({g for _, g, _ in _S.history})
    t_end = _now()
    fig, axes = plt.subplots(len(gpios), 1, sharex=True, figsize=(9, 1.3 * len(gpios) + 0.6), squeeze=False)
    for ax, g in zip(axes[:, 0], gpios):
        name, active_low = _S.names.get(g, ("GPIO{}".format(g), False))
        pts = [(t, v) for t, gg, v in _S.history if gg == g]
        if not pts:
            continue
        xs, ys = [], []
        for t, v in pts:
            if isinstance(v, float):
                y = (1 - v) if active_low else v
            else:
                y = (1 - v) if active_low else v
            xs.append(t / 1000.0)
            ys.append(y)
        xs.append(t_end / 1000.0)
        ys.append(ys[-1])
        ax.step(xs, ys, where="post", color="#a3262b", linewidth=2)
        ax.set_ylim(-0.2, 1.3)
        ax.set_yticks([0, 1])
        ax.set_ylabel(name + (" (ON)" if active_low else ""), rotation=0, ha="right", va="center")
        ax.grid(axis="x", linestyle=":", alpha=0.6)
    axes[-1, 0].set_xlabel("czas [s]")
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(filename, dpi=120)
    plt.close(fig)
    print("[SIM] Zapisano przebieg do pliku", filename)
