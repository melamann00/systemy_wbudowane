import machine
import time
import math

TOL = 1e-12
MAX_ITER = 100

def pierwiastek_newton(a, tol=TOL):
    if a < 0:
        raise ValueError("liczba nie moze byc ujemna")
    if a == 0:
        return 0.0, 0
    if a >= 1:
        x = a
    else:
        x = 1.0
    for i in range(1, MAX_ITER + 1):
        nowy = 0.5 * (x + a / x)
        if abs(nowy - x) <= tol * nowy:
            return nowy, i
        x = nowy
    return x, MAX_ITER

def testy():
    assert pierwiastek_newton(0)[0] == 0.0
    assert abs(pierwiastek_newton(1)[0] - 1.0) < 1e-9
    assert abs(pierwiastek_newton(4)[0] - 2.0) < 1e-9
    assert abs(pierwiastek_newton(2)[0] - 1.41421356237) < 1e-9
    assert abs(pierwiastek_newton(0.25)[0] - 0.5) < 1e-9 
    assert abs(pierwiastek_newton(1e12)[0] - 1e6) < 1e-3
    assert abs(pierwiastek_newton(123.456)[0] - math.sqrt(123.456)) < 1e-9
    print("Testy OK")

def zmierz(powtorzenia):
    t0 = time.ticks_ms()
    for k in range(1, powtorzenia + 1):
        pierwiastek_newton(k)
    czas_n = time.ticks_diff(time.ticks_ms(), t0)
    t0 = time.ticks_ms()
    for k in range(1, powtorzenia + 1):
        math.sqrt(k)
    czas_m = time.ticks_diff(time.ticks_ms(), t0)
    return czas_n, czas_m

def main():
    for a in (2, 144, 0.01): 
        x, it = pierwiastek_newton(a)
        print("sqrt({}) = {:.10f} (Newton, {} iteracji), math.sqrt = {:.10f}".format(a, x, it, math.sqrt(a)))
    n = 20000
    cn, cm = zmierz(n)
    print("{} pierwiastkow: Newton {} ms, math.sqrt {} ms".format(n, cn, cm))
    while True:
        tekst = input("Podaj liczbe (q - koniec): ").strip().replace(",", ".")
        if tekst.lower() == "q":
            break
        try:
            a = float(tekst)
            x, it = pierwiastek_newton(a)
            print("Newton: {:.10f} ({} iteracji), math.sqrt: {:.10f}, roznica: {:.2e}".format(x, it, math.sqrt(a), abs(x - math.sqrt(a))))
        except ValueError as e:
            print("Blad:", e)

testy()
main()
