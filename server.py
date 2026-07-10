from flask import Flask, request, jsonify, send_from_directory
import requests
import os
import re
import traceback
import threading
import time
import json
from datetime import datetime, timezone

app = Flask(__name__, static_folder='static')

@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
NEWS_API_KEY      = os.environ.get('NEWS_API_KEY', 'babbb951d220490a81cccfd354d348c2')
TG_TOKEN          = os.environ.get('TG_TOKEN', '8947905331:AAGq8NINPfkVHgpQU2muN8G690qMhm0xR6M')
TG_CHAT           = os.environ.get('TG_CHAT', '1673781813')
ALPACA_KEY        = os.environ.get('ALPACA_KEY', '')
ALPACA_SECRET     = os.environ.get('ALPACA_SECRET', '')
ALPACA_BASE       = 'https://paper-api.alpaca.markets/v2'

last_signal        = None
last_signal_time   = None
SIGNAL_COOLDOWN_H  = 4
alerts_only_mode   = False
trades_today       = 0
trades_today_date  = None
MAX_TRADES_DAY     = 10
equity_start_day   = None
capital_history    = []

def save_state_telegram():
    try:
        msg = f"__STATE__:{last_signal}:{last_signal_time or 'None'}"
        requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT, 'text': msg},
            timeout=10
        )
    except:
        pass

def load_state_telegram():
    global last_signal, last_signal_time
    try:
        r = requests.get(
            f'https://api.telegram.org/bot{TG_TOKEN}/getUpdates',
            params={'limit': 100, 'offset': -100},
            timeout=10
        )
        messages = r.json().get('result', [])
        for update in reversed(messages):
            text = update.get('message', {}).get('text', '')
            if text.startswith('__STATE__:'):
                parts = text.split(':')
                if len(parts) >= 3:
                    last_signal = parts[1] if parts[1] != 'None' else None
                    last_signal_time = parts[2] if parts[2] != 'None' else None
                    print(f"[State] Recuperado: last_signal={last_signal} last_time={last_signal_time}")
                    return
    except Exception as e:
        print(f"[State] Error cargando desde Telegram: {e}")

def should_send_alert(signal_type):
    if signal_type != last_signal:
        return True
    if last_signal_time is None:
        return True
    try:
        last_time = datetime.fromisoformat(last_signal_time)
        hours_passed = (datetime.now(timezone.utc) - last_time).total_seconds() / 3600
        return hours_passed >= SIGNAL_COOLDOWN_H
    except:
        return True

def alpaca_headers():
    return {
        'APCA-API-KEY-ID': ALPACA_KEY,
        'APCA-API-SECRET-KEY': ALPACA_SECRET,
        'Content-Type': 'application/json'
    }

def is_market_open():
    try:
        r = requests.get(f'{ALPACA_BASE}/clock', headers=alpaca_headers(), timeout=10)
        if r.status_code == 200:
            return r.json().get('is_open', False)
    except:
        pass
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    market_open  = now.replace(hour=13, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=20, minute=0,  second=0, microsecond=0)
    return market_open <= now <= market_close

def send_telegram(msg):
    try:
        requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT, 'text': msg, 'parse_mode': 'Markdown'},
            timeout=10
        )
    except Exception as e:
        print(f"Telegram error: {e}")

def fetch_gold_price():
    try:
        r = requests.get('https://api.gold-api.com/price/XAU', timeout=10)
        d = r.json()
        return {'price': d['price'], 'open': d.get('prev_close_price', d['price']*0.999)}
    except:
        return {'price': 4000.0, 'open': 3996.0, 'sim': True}

def fetch_dxy():
    try:
        r = requests.get('https://api.gold-api.com/price/DXY', timeout=10)
        d = r.json()
        return {'value': d['price'], 'change': d['price'] - d.get('prev_close_price', d['price'])}
    except:
        return {'value': 104.2, 'change': 0.02}

def fetch_news():
    try:
        q = 'gold price OR "XAU/USD" OR "Federal Reserve" OR inflation'
        url = f'https://newsapi.org/v2/everything?q={requests.utils.quote(q)}&language=en&sortBy=publishedAt&pageSize=4&apiKey={NEWS_API_KEY}'
        r = requests.get(url, timeout=10)
        d = r.json()
        return [a.get('title','') for a in d.get('articles', [])[:4]]
    except:
        return []

def analyze_signal(price, dxy, news):
    news_text = '\n'.join([f'{i+1}. {n}' for i, n in enumerate(news)]) if news else 'Sin noticias'
    prompt = f"""Eres analista experto en XAU/USD. Analiza y genera señal de trading.
PRECIO: ${price['price']:.2f} | Apertura: ${price['open']:.2f} | Cambio: {((price['price']-price['open'])/price['open']*100):.2f}%
DXY: {dxy['value']:.2f} ({'+' if dxy['change']>=0 else ''}{dxy['change']:.2f})
NOTICIAS:
{news_text}
CONTEXTO: Oro en zona $3800-4200, Fed con tasas 4.25-4.5%, bancos centrales comprando oro.
Responde SOLO JSON sin backticks:
{{"signal":"COMPRAR o VENDER o ESPERAR","confidence":número 40-93,"entry":número,"takeProfit":número,"stopLoss":número,"rrRatio":número,"reasoning":"2 frases en español","timeframe":"intradía o corto plazo"}}"""
    r = requests.post(
        'https://api.anthropic.com/v1/messages',
        headers={'Content-Type': 'application/json', 'x-api-key': ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01'},
        json={'model': 'claude-haiku-4-5-20251001', 'max_tokens': 500, 'messages': [{'role': 'user', 'content': prompt}]},
        timeout=30
    )
    if r.status_code != 200:
        raise Exception(f'API error {r.status_code}')
    text = ''.join([b.get('text','') for b in r.json().get('content',[])])
    text = text.strip().replace('```json','').replace('```','').strip()
    match = re.search(r'\{[\s\S]*\}', text)
    if not match:
        raise Exception('No JSON in response')
    return json.loads(match.group(0))

def get_alpaca_equity():
    try:
        r = requests.get(f'{ALPACA_BASE}/account', headers=alpaca_headers(), timeout=10)
        return float(r.json().get('equity', 0))
    except:
        return 0.0

def close_all_positions(reason='Señal cambiada'):
    if not ALPACA_KEY:
        return
    try:
        positions = requests.get(f'{ALPACA_BASE}/positions', headers=alpaca_headers(), timeout=10).json()
        if not positions:
            return
        for pos in positions:
            symbol = pos.get('symbol')
            qty    = abs(float(pos.get('qty', 0)))
            side   = pos.get('side')
            close_side = 'sell' if side == 'long' else 'buy'
            pl     = float(pos.get('unrealized_pl', 0))
            requests.post(
                f'{ALPACA_BASE}/orders',
                headers=alpaca_headers(),
                json={'symbol': symbol, 'qty': str(int(qty)), 'side': close_side, 'type': 'market', 'time_in_force': 'day'},
                timeout=10
            )
            emoji = '💚' if pl >= 0 else '❤️'
            send_telegram(
                f"{emoji} *POSICIÓN CERRADA*\n\n"
                f"📌 *Símbolo:* {symbol}\n"
                f"📦 *Cantidad:* {int(qty)} acciones\n"
                f"{'✅' if pl >= 0 else '❌'} *P&L:* ${pl:+.2f}\n"
                f"📝 *Motivo:* {reason}\n\n"
                f"📱 [Ver en Alpaca](https://app.alpaca.markets)"
            )
    except Exception as e:
        print(f"[Alpaca] Error cerrando posiciones: {e}")

def execute_alpaca_order(signal, confidence, sl_price, tp_price):
    global trades_today, trades_today_date, equity_start_day
    if not ALPACA_KEY or not ALPACA_SECRET:
        return None
    if alerts_only_mode:
        return None
    if confidence < 65:
        return None
    if not is_market_open():
        print("[Alpaca] Mercado cerrado — orden no ejecutada")
        return None
    today = datetime.now(timezone.utc).date()
    if trades_today_date != today:
        trades_today = 0
        trades_today_date = today
    if trades_today >= MAX_TRADES_DAY:
        send_telegram(f"⛔ *Límite diario alcanzado*: {MAX_TRADES_DAY} trades hoy.")
        return None
    try:
        account = requests.get(f'{ALPACA_BASE}/account', headers=alpaca_headers(), timeout=10).json()
        equity  = float(account.get('equity', 0))
        if equity <= 0:
            return None
        if equity_start_day is None:
            equity_start_day = equity
        if equity_start_day > 0:
            drawdown = (equity_start_day - equity) / equity_start_day * 100
            if drawdown >= 5.0:
                send_telegram(f"🚨 *BOT PAUSADO — DRAWDOWN {drawdown:.1f}%*\n\nCapital: ${equity:,.2f}\n📱 [Ver en Alpaca](https://app.alpaca.markets)")
                return None
        quote_resp = requests.get(f'{ALPACA_BASE}/stocks/GLD/quotes/latest', headers=alpaca_headers(), timeout=10)
        gld_price  = float(quote_resp.json().get('quote', {}).get('ap', 200))
        if gld_price <= 0:
            gld_price = 200.0
        risk_amount = equity * 0.01
        qty = max(1, int(risk_amount / gld_price))
        side = 'buy' if signal == 'COMPRAR' else 'sell'
        order_resp = requests.post(
            f'{ALPACA_BASE}/orders',
            headers=alpaca_headers(),
            json={'symbol': 'GLD', 'qty': str(qty), 'side': side, 'type': 'market', 'time_in_force': 'day'},
            timeout=10
        )
        order        = order_resp.json()
        order_id     = order.get('id', 'N/A')
        order_status = order.get('status', 'unknown')
        trades_today += 1
        emoji = '🟢' if side == 'buy' else '🔴'
        send_telegram(
            f"{emoji} *ORDEN ALPACA EJECUTADA*\n\n"
            f"📌 *Acción:* {'COMPRA' if side == 'buy' else 'VENTA'} GLD\n"
            f"📦 *Cantidad:* {qty} acciones\n"
            f"📊 *Confianza IA:* {confidence}%\n"
            f"💰 *Capital:* ${equity:,.2f}\n"
            f"📈 *Trades hoy:* {trades_today}/{MAX_TRADES_DAY}\n"
            f"🆔 *ID:* `{order_id}`\n"
            f"✅ *Estado:* {order_status}\n\n"
            f"_GLD = ETF que sigue el precio del oro_\n\n"
            f"📱 [Ver en Alpaca](https://app.alpaca.markets)"
        )
        capital_history.append({'time': datetime.now(timezone.utc).isoformat(), 'equity': equity})
        if len(capital_history) > 500:
            capital_history.pop(0)
        return order_id
    except Exception as e:
        print(f"[Alpaca] Error: {traceback.format_exc()}")
        return None

def monitor_positions():
    if not ALPACA_KEY:
        return
    try:
        positions = requests.get(f'{ALPACA_BASE}/positions', headers=alpaca_headers(), timeout=10).json()
        for pos in positions:
            pl_pct = float(pos.get('unrealized_plpc', 0)) * 100
            symbol = pos.get('symbol')
            pl     = float(pos.get('unrealized_pl', 0))
            if pl_pct <= -2.0:
                send_telegram(
                    f"⚠️ *ALERTA POSICIÓN EN PÉRDIDA*\n\n"
                    f"📌 *Símbolo:* {symbol}\n"
                    f"❌ *P&L:* ${pl:+.2f} ({pl_pct:.1f}%)\n\n"
                    f"📱 [Ver en Alpaca](https://app.alpaca.markets)"
                )
    except Exception as e:
        print(f"[Monitor] Error: {e}")

def daily_summary():
    global equity_start_day, trades_today, trades_today_date
    try:
        account   = requests.get(f'{ALPACA_BASE}/account', headers=alpaca_headers(), timeout=10).json()
        equity    = float(account.get('equity', 0))
        cash      = float(account.get('cash', 0))
        positions = requests.get(f'{ALPACA_BASE}/positions', headers=alpaca_headers(), timeout=10).json()
        pl_day = equity - equity_start_day if equity_start_day else 0
        pl_pct = (pl_day / equity_start_day * 100) if equity_start_day else 0
        pos_txt = f"{len(positions)} posición(es) abierta(s)" if positions else "Sin posiciones abiertas"
        send_telegram(
            f"📊 *RESUMEN DIARIO AURUM v2*\n"
            f"_{datetime.now(timezone.utc).strftime('%d/%m/%Y · %H:%M UTC')}_\n\n"
            f"💰 *Capital:* ${equity:,.2f}\n"
            f"💵 *Cash:* ${cash:,.2f}\n"
            f"{'✅' if pl_day >= 0 else '❌'} *P&L hoy:* ${pl_day:+.2f} ({pl_pct:+.1f}%)\n"
            f"📈 *Trades hoy:* {trades_today}/{MAX_TRADES_DAY}\n"
            f"📌 *{pos_txt}*\n\n"
            f"📱 [Ver en Alpaca](https://app.alpaca.markets)"
        )
        equity_start_day   = equity
        trades_today       = 0
        trades_today_date  = datetime.now(timezone.utc).date()
    except Exception as e:
        print(f"[Daily] Error: {e}")

def auto_analysis():
    global last_signal, last_signal_time  # ← AQUÍ ESTABA EL BUG
    print(f"[{datetime.now()}] Running auto analysis...")
    try:
        price = fetch_gold_price()
        dxy   = fetch_dxy()
        news  = fetch_news()
        sig   = analyze_signal(price, dxy, news)
        confidence  = sig.get('confidence', 0)
        signal_type = sig.get('signal', 'ESPERAR')
        print(f"Signal: {signal_type} ({confidence}%) | Last: {last_signal} | Should send: {should_send_alert(signal_type)}")

        if signal_type != 'ESPERAR' and confidence >= 65 and should_send_alert(signal_type):
            if last_signal and last_signal != signal_type:
                close_all_positions(reason=f'Señal cambió a {signal_type}')
            emoji = '🟢' if signal_type == 'COMPRAR' else '🔴'
            send_telegram(
                f"{emoji} *AURUM v2 · XAU/USD*\n\n"
                f"📌 *Acción:* {signal_type}\n"
                f"💰 *Precio:* ${price['price']:.2f}\n"
                f"🎯 *Entrada:* ${sig.get('entry', 0):.2f}\n"
                f"✅ *TP:* ${sig.get('takeProfit', 0):.2f}\n"
                f"🛑 *SL:* ${sig.get('stopLoss', 0):.2f}\n"
                f"⚖️ *R:R:* {sig.get('rrRatio', 0):.1f}:1\n"
                f"📊 *Confianza:* {confidence}%\n"
                f"⏱ *Horizonte:* {sig.get('timeframe', 'intradía')}\n\n"
                f"💬 _{sig.get('reasoning', '')}_\n\n"
                f"_⚠️ No es asesoramiento financiero._"
            )
            last_signal      = signal_type
            last_signal_time = datetime.now(timezone.utc).isoformat()
            save_state_telegram()
            print(f"Alert sent: {signal_type} at {last_signal_time}")
            execute_alpaca_order(
                signal=signal_type,
                confidence=confidence,
                sl_price=sig.get('stopLoss', 0),
                tp_price=sig.get('takeProfit', 0)
            )
        elif signal_type == 'ESPERAR':
            last_signal      = None
            last_signal_time = None
            save_state_telegram()
        monitor_positions()
    except Exception as e:
        print(f"Auto analysis error: {traceback.format_exc()}")

def scheduler():
    time.sleep(120)
    last_daily = None
    while True:
        now_utc = datetime.now(timezone.utc)
        if now_utc.hour == 8 and now_utc.minute < 15:
            today = now_utc.date()
            if last_daily != today:
                daily_summary()
                last_daily = today
        auto_analysis()
        time.sleep(900)

load_state_telegram()
scheduler_thread = threading.Thread(target=scheduler, daemon=True)
scheduler_thread.start()
print("Auto-scheduler started (every 15 min)")

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/api/signal', methods=['POST', 'OPTIONS'])
def get_signal():
    if request.method == 'OPTIONS':
        return '', 200
    try:
        data     = request.json
        prompt   = data.get('prompt', '')
        response = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={'Content-Type': 'application/json', 'x-api-key': ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01'},
            json={'model': 'claude-haiku-4-5-20251001', 'max_tokens': 1000, 'messages': [{'role': 'user', 'content': prompt}]},
            timeout=30
        )
        if response.status_code != 200:
            return jsonify({'error': f'API error {response.status_code}: {response.text}'}), 500
        text = ''.join([b.get('text', '') for b in response.json().get('content', [])])
        return jsonify({'text': text})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/news', methods=['GET', 'OPTIONS'])
def get_news():
    if request.method == 'OPTIONS':
        return '', 200
    try:
        q   = 'gold price OR "XAU/USD" OR "gold market" OR "Federal Reserve" OR inflation'
        url = f'https://newsapi.org/v2/everything?q={requests.utils.quote(q)}&language=en&sortBy=publishedAt&pageSize=6&apiKey={NEWS_API_KEY}'
        r   = requests.get(url, timeout=10)
        if not r.ok:
            return jsonify({'articles': [], 'error': f'NewsAPI {r.status_code}'}), 200
        articles = r.json().get('articles', [])
        clean    = [{'headline': a.get('title',''), 'source': a.get('source',{}).get('name',''), 'publishedAt': a.get('publishedAt','')} for a in articles]
        return jsonify({'articles': clean})
    except Exception as e:
        return jsonify({'articles': [], 'error': str(e)}), 200

@app.route('/api/status', methods=['GET'])
def status():
    return jsonify({
        'status':             'ok',
        'scheduler':          'running',
        'last_signal':        last_signal,
        'last_signal_time':   last_signal_time,
        'alerts_only_mode':   alerts_only_mode,
        'trades_today':       trades_today,
        'max_trades_day':     MAX_TRADES_DAY,
        'alpaca_configured':  bool(ALPACA_KEY and ALPACA_SECRET),
        'time':               datetime.now(timezone.utc).isoformat()
    })

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'key_set': bool(ANTHROPIC_API_KEY)})

@app.route('/api/alpaca/status', methods=['GET'])
def alpaca_status():
    if not ALPACA_KEY or not ALPACA_SECRET:
        return jsonify({'error': 'Alpaca no configurado'})
    try:
        account   = requests.get(f'{ALPACA_BASE}/account',                    headers=alpaca_headers(), timeout=10).json()
        positions = requests.get(f'{ALPACA_BASE}/positions',                  headers=alpaca_headers(), timeout=10).json()
        orders    = requests.get(f'{ALPACA_BASE}/orders?status=all&limit=20', headers=alpaca_headers(), timeout=10).json()
        equity    = float(account.get('equity', 0))
        pl_day    = equity - equity_start_day if equity_start_day else 0
        return jsonify({
            'equity':          account.get('equity'),
            'cash':            account.get('cash'),
            'buying_power':    account.get('buying_power'),
            'pl_day':          round(pl_day, 2),
            'trades_today':    trades_today,
            'alerts_only':     alerts_only_mode,
            'positions':       positions,
            'recent_orders':   orders,
            'capital_history': capital_history[-50:]
        })
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/api/alpaca/mode', methods=['POST', 'OPTIONS'])
def set_mode():
    global alerts_only_mode
    if request.method == 'OPTIONS':
        return '', 200
    data             = request.json or {}
    alerts_only_mode = bool(data.get('alerts_only', False))
    mode_txt         = 'SOLO ALERTAS' if alerts_only_mode else 'TRADING ACTIVO'
    send_telegram(f"⚙️ *Modo cambiado:* {mode_txt}")
    return jsonify({'alerts_only_mode': alerts_only_mode, 'message': f'Modo: {mode_txt}'})

@app.route('/api/alpaca/close_all', methods=['POST', 'OPTIONS'])
def close_all():
    if request.method == 'OPTIONS':
        return '', 200
    close_all_positions(reason='Cierre manual desde dashboard')
    return jsonify({'message': 'Posiciones cerradas'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
