import asyncio
import random
import time
import uuid
import logging
import io
import json
from functools import wraps
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.utils import executor
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from supabase import create_client, Client
from flask import Flask
from threading import Thread

# --- НАСТРОЙКИ ---
TOKEN = "ТВОЙ_ТОКЕН_БОТА" 
ADMINS = [5033063588, 1827568041, 6408844545]

SUPABASE_URL = "ТВОЙ_SUPABASE_URL" 
SUPABASE_KEY = "ТВОЙ_SUPABASE_ANON_KEY"
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# Состояния
class AddCard(StatesGroup):
    name = State()
    rarity = State()
    price = State()
    photo = State()

class OrderCreation(StatesGroup):
    title = State()
    description = State()
    reward_type = State()
    reward_value = State()
    photo = State()

class OrderExecution(StatesGroup):
    waiting_photo = State()

# Константы
RARITIES = {
    "mystic": "⚫ Мистический",
    "legendary": "🟡 Легендарный",
    "mythic": "🔴 Мифический",
    "epic": "🟣 Эпический",
    "rare": "🟢 Редкий",
    "super_common": "🔵 Сверх-обычный",
    "common": "⚪ Обычный"
}

CHANCES = {
    "mystic": 1, "legendary": 2, "mythic": 5, "epic": 10,
    "rare": 30, "super_common": 40, "common": 60
}

PAGE_SIZE = 10
MARKET_PAGE_SIZE = 3

VIP_STATUSES = {
    "vip": {"name": "👑 VIP", "multiplier": 1.5, "cd_reduction": 10, "emoji": "👑"},
    "premium": {"name": "💎 Premium", "multiplier": 1.7, "cd_reduction": 15, "emoji": "💎"},
    "platinum": {"name": "🌟 Platinum VIP", "multiplier": 1.9, "cd_reduction": 20, "emoji": "🌟"}
}

ORDER_LIFETIME = 3 * 24 * 3600
DUEL_REQUEST_TIMEOUT = 60
DUEL_CHOICE_TIMEOUT = 30
DUEL_LEVEL_PRICES = [0, 10000, 20000, 40000, 80000, 160000, 320000, 640000, 1280000, 2560000, 5120000]

duel_requests = {}
active_duels = {}
duel_counter = 0

# --- РАБОТА С SUPABASE ---
def get_user(uid: str, name: str = None):
    res = supabase.table("users").select("*").eq("uid", uid).execute()
    if res.data:
        user = res.data[0]
        if name and user.get("name") != name:
            user["name"] = name
            supabase.table("users").update({"name": name}).eq("uid", uid).execute()
        return user
    else:
        new_user = {
            "uid": uid, "name": name or "Unknown", "balance": 0, "cards": [],
            "last_roll": 0, "vip_status": None, "duel_level": 0
        }
        supabase.table("users").insert(new_user).execute()
        return new_user

def save_user(uid: str, user_data: dict):
    updates = {
        "balance": user_data.get("balance", 0),
        "cards": user_data.get("cards", []),
        "last_roll": user_data.get("last_roll", 0),
        "vip_status": user_data.get("vip_status"),
        "duel_level": user_data.get("duel_level", 0)
    }
    supabase.table("users").update(updates).eq("uid", str(uid)).execute()

def get_all_cards():
    res = supabase.table("cards").select("*").execute()
    data = {r: [] for r in RARITIES}
    data["prices"] = {}
    for row in res.data:
        if row["rarity"] in data:
            data[row["rarity"]].append({"name": row["name"], "image": row["image"]})
        data["prices"][row["name"]] = row["price"]
    return data

def get_status_emoji(user_data):
    if user_data.get("vip_status"):
        s = VIP_STATUSES.get(user_data["vip_status"])
        if s: return s["emoji"] + " "
    return ""

def get_duel_win_chance(level1, level2):
    return max(10, min(90, 50 + (level1 - level2) * 10))

# --- АДМИН-ПАНЕЛЬ ---
def admin_only(func):
    @wraps(func)
    async def wrapper(message: types.Message, *args, **kwargs):
        if message.from_user.id not in ADMINS: return
        if message.chat.type != 'private':
            return await message.answer("Эта команда доступна только в личных сообщениях с ботом.")
        return await func(message, *args, **kwargs)
    return wrapper

ADMIN_COMMANDS = {
    "💰 Экономика": [("/add_money <id> <сумма>", "Начислить коины"), ("/sub_money <id> <сумма>", "Снять коины")],
    "👤 Пользователи": [("/reset_cd <id>", "Сбросить кулдаун тпозера"), ("/clear_cards <id>", "Очистить инвентарь"), ("/setstatus <id> <status>", "Выдать VIP"), ("/removestatus <id>", "Снять VIP"), ("/rescd", "Сбросить кулдаун всем")],
    "🃏 Карты": [("/add", "Добавить новую карту (ЛС)")],
    "🏪 Рынок": [("/clear_market", "Полностью очистить рынок")],
    "🎫 Промокоды": [("/create_promo <статус> <макс_исп> <код>", "Создать промокод"), ("/delete_promo <код>", "Удалить промокод"), ("/list_promo", "Активные промокоды")],
    "📜 Заказы": [("/zakaz", "Создать новый заказ (ЛС)")],
    "📊 Статистика": [("/stats_bot", "Общая статистика бота")],
    "📢 Рассылка": [("/announce <текст>", "Разослать объявление всем")]
}

def build_admin_menu(page: str = "main"):
    if page == "main":
        text = "🛠 <b>Админ‑панель</b>\n\nВыберите категорию:"
        kb = InlineKeyboardMarkup(row_width=2)
        for cat in ADMIN_COMMANDS: kb.insert(InlineKeyboardButton(cat, callback_data=f"admcat_{cat}"))
        return text, kb
    else:
        cmds = ADMIN_COMMANDS.get(page, [])
        text = f"📂 <b>{page}</b>\n\n"
        for cmd, desc in cmds: text += f"<code>{cmd}</code>\n  — {desc}\n\n"
        kb = InlineKeyboardMarkup()
        for cmd, _ in cmds: kb.add(InlineKeyboardButton(cmd, switch_inline_query_current_chat=cmd))
        kb.add(InlineKeyboardButton("🔙 Назад", callback_data="admcat_main"))
        return text, kb

@dp.message_handler(commands=['admin'])
@admin_only
async def admin_panel(message: types.Message):
    text, kb = build_admin_menu("main")
    await message.answer(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("admcat_"))
async def process_admin_category(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS: return await callback.answer("Нет доступа.", show_alert=True)
    page = callback.data.split("admcat_", 1)[1]
    text, kb = build_admin_menu(page)
    try: await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except: pass
    await callback.answer()

# --- ВСПОМОГАТЕЛЬНЫЕ ---
def get_main_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("🎲 Тпозер", "📦 Инвентарь", "📊 Статистика", "🏆 Топ", "🏪 Рынок", "💫 Статусы", "⚔️ Дуэль", "📜 Заказы")
    return kb

def get_inventory_text(user_data, page=0):
    cards = user_data.get("cards", [])
    total = len(cards)
    if total == 0:
        return (f"👤 <b>{user_data.get('name', 'Игрок')}</b>\n💰 Баланс: <code>{user_data.get('balance', 0)}</code>\n\n📭 <b>У тебя пока нет карт.</b>"), 0, 0

    max_page = (total - 1) // PAGE_SIZE
    page = max(0, min(page, max_page))
    start, end = page * PAGE_SIZE, min((page + 1) * PAGE_SIZE, total)

    text = f"👤 <b>{user_data.get('name', 'Игрок')}</b>\n💰 Баланс: <code>{user_data.get('balance', 0)}</code>\n"
    if user_data.get("vip_status"): text += f"✨ Статус: {VIP_STATUSES.get(user_data['vip_status'], {}).get('name', '')}\n"
    text += f"⚔️ Уровень дуэли: {user_data.get('duel_level', 0)}\n\n<b>Твои карты (страница {page+1}/{max_page+1}):</b>\n"
    
    for idx in range(start, end):
        card = cards[idx]
        text += f"{idx+1}. {RARITIES.get(card['rarity'], '⚪ Обычный')} — <b>{card['name']}</b>\n"

    nav = ""
    if page > 0: nav += f"◀️ Предыдущая страница: /inventory {page}\n"
    if page < max_page: nav += f"▶️ Следующая страница: /inventory {page+2}\n"
    if nav: text += "\n" + nav
    return text, page + 1, max_page + 1

# --- ИГРОВЫЕ БАЗОВЫЕ КОМАНДЫ ---
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    await message.answer("👋 Привет! Я бот для коллекционирования позеров.\nНабери 'тпозер' для получения случайной карты!", reply_markup=get_main_keyboard() if message.chat.type == 'private' else None)

@dp.message_handler(lambda m: m.text and m.text.lower().strip() == "тпозер")
async def roll_card(message: types.Message):
    uid = str(message.from_user.id)
    user = get_user(uid, message.from_user.first_name)
    now = int(time.time())

    base_cd = 3600
    if user.get("vip_status") and user["vip_status"] in VIP_STATUSES:
        base_cd -= VIP_STATUSES[user["vip_status"]]["cd_reduction"] * 60
    base_cd = max(base_cd, 0)
    
    if message.from_user.id not in ADMINS and now - user["last_roll"] < base_cd:
        return await message.answer(f"⏳ Рано! Жди <b>{(base_cd - (now - user['last_roll'])) // 60} мин.</b>", parse_mode="HTML")

    cards_db = get_all_cards()
    owned_names = {c['name'] for c in user.get('cards', [])}
    available_by_rarity = {r: [c for c in cards if c['name'] not in owned_names] for r, cards in cards_db.items() if r in RARITIES and [c for c in cards if c['name'] not in owned_names]}

    if not available_by_rarity: return await message.answer("🎉 Поздравляю! Ты собрал все карты!")

    rarities = list(available_by_rarity.keys())
    chosen_rarity = random.choices(rarities, weights=[CHANCES[r] for r in rarities])[0]
    card = random.choice(available_by_rarity[chosen_rarity])
    price = cards_db.get("prices", {}).get(card["name"], 10)

    user["cards"].append({"name": card["name"], "rarity": chosen_rarity})
    user["last_roll"] = now
    save_user(uid, user)

    mult = VIP_STATUSES[user["vip_status"]]["multiplier"] if user.get("vip_status") else 1.0
    caption = (f"👤 <b>{message.from_user.first_name}</b> призывает...\n━━━━━━━━━━━━━━\n"
               f"🃏 Карта: <b>{card['name']}</b>\n💎 Редкость: <b>{RARITIES[chosen_rarity]}</b>\n💰 Базовая цена: <b>{price}</b>\n"
               f"━━━━━━━━━━━━━━\n💡 Продажа: /sellposer даст примерно {int(price * 0.8 * mult)}")

    try: await bot.send_photo(message.chat.id, photo=card['image'], caption=caption, parse_mode="HTML")
    except: await message.answer(caption + "\n\n⚠️ Изображение отсутствует в БД.", parse_mode="HTML")

@dp.message_handler(commands=['inventory'])
async def show_inventory(message: types.Message):
    args = message.get_args()
    page = int(args) - 1 if args and args.isdigit() else 0
    user = get_user(str(message.from_user.id), message.from_user.first_name)
    text, _, _ = get_inventory_text(user, page)
    await message.answer(text, parse_mode="HTML")

@dp.message_handler(commands=['sellposer'])
async def sell_card(message: types.Message):
    args = message.get_args()
    if not args or not args.isdigit(): return await message.answer("❌ Пример: /sellposer 5")
    idx = int(args) - 1
    uid = str(message.from_user.id)
    user = get_user(uid, message.from_user.first_name)
    
    if not (0 <= idx < len(user["cards"])): return await message.answer("❌ Карта не найдена.")

    card = user["cards"].pop(idx)
    base_price = get_all_cards().get("prices", {}).get(card["name"], 10)
    mult = VIP_STATUSES[user["vip_status"]]["multiplier"] if user.get("vip_status") else 1.0
    sell_price = int(base_price * 0.8 * mult)

    user["balance"] += sell_price
    save_user(uid, user)
    await message.answer(f"✅ Продано <b>{card['name']}</b> за {sell_price}!\n💰 Новый баланс: {user['balance']}", parse_mode="HTML")
    text, _, _ = get_inventory_text(user)
    await message.answer(text, parse_mode="HTML")

@dp.message_handler(commands=['torgposer'])
async def place_on_market(message: types.Message):
    args = message.get_args().split()
    if len(args) != 2 or not args[0].isdigit() or not args[1].isdigit(): return await message.answer("❌ Пример: /torgposer 5 1500")
    idx, price = int(args[0]) - 1, int(args[1])
    uid = str(message.from_user.id)
    user = get_user(uid, message.from_user.first_name)
    
    if not (0 <= idx < len(user["cards"])): return await message.answer("❌ Карта не найдена.")
    card = user["cards"].pop(idx)
    save_user(uid, user)

    supabase.table("market").insert({
        "id": uuid.uuid4().hex[:12], "seller_id": uid, "seller_name": message.from_user.first_name,
        "card_name": card["name"], "card_rarity": card["rarity"], "price": price, "timestamp": time.time()
    }).execute()
    await message.answer(f"✅ Карта <b>{card['name']}</b> выставлена за {price}!", parse_mode="HTML")

@dp.message_handler(commands=['market'])
async def show_market(message: types.Message):
    args = message.get_args()
    page = int(args) - 1 if args and args.isdigit() else 0
    items = supabase.table("market").select("*").order("timestamp", desc=False).execute().data
    if not items: return await message.answer("🏪 <b>Рынок пуст</b>", parse_mode="HTML")

    max_page = (len(items) - 1) // MARKET_PAGE_SIZE
    page = max(0, min(page, max_page))
    start, end = page * MARKET_PAGE_SIZE, min((page + 1) * MARKET_PAGE_SIZE, len(items))

    text = f"🏪 <b>Рынок (стр. {page+1}/{max_page+1})</b>\n\n"
    for idx in range(start, end):
        it = items[idx]
        text += f"{idx + 1}. <b>{it['card_name']}</b> {RARITIES.get(it['card_rarity'], '⚪')}\n   👤 {it['seller_name']}\n   💰 {it['price']}\n\n"

    nav = ""
    if page > 0: nav += f"◀ Предыдущая: /market {page}\n"
    if page < max_page: nav += f"▶️ Следующая: /market {page+2}\n"
    await message.answer(text + nav + "\n➡️ /buy <номер>", parse_mode="HTML")

@dp.message_handler(commands=['buy'])
async def buy_card(message: types.Message):
    args = message.get_args()
    if not args or not args.isdigit(): return await message.answer("❌ Пример: /buy 5")
    idx = int(args) - 1
    items = supabase.table("market").select("*").order("timestamp", desc=False).execute().data
    
    if not (0 <= idx < len(items)): return await message.answer("❌ Объявление не найдено.")
    offer = items[idx]
    buyer_id = str(message.from_user.id)
    if buyer_id == offer['seller_id']: return await message.answer("❌ Нельзя купить свою карту.")

    buyer = get_user(buyer_id, message.from_user.first_name)
    if buyer["balance"] < offer['price']: return await message.answer("❌ Недостаточно средств.")

    buyer["balance"] -= offer['price']
    buyer["cards"].append({"name": offer['card_name'], "rarity": offer['card_rarity']})
    save_user(buyer_id, buyer)
    
    seller = get_user(offer['seller_id'])
    seller["balance"] += offer['price']
    save_user(offer['seller_id'], seller)

    supabase.table("market").delete().eq("id", offer["id"]).execute()
    await message.answer("✅ Покупка успешна!")
    try: await bot.send_message(offer['seller_id'], f"💰 Карта <b>{offer['card_name']}</b> продана за {offer['price']}!", parse_mode="HTML")
    except: pass

@dp.message_handler(commands=['trade'])
async def trade_card(message: types.Message):
    if not message.reply_to_message: return await message.answer("Ответь на сообщение получателя.")
    if message.from_user.id == message.reply_to_message.from_user.id: return await message.answer("Нельзя передать самому себе.")
    
    args = message.get_args()
    if not args or not args.isdigit(): return await message.answer("Пример: /trade 1")
    idx = int(args) - 1

    from_user = get_user(str(message.from_user.id), message.from_user.first_name)
    to_user = get_user(str(message.reply_to_message.from_user.id), message.reply_to_message.from_user.first_name)

    if not (0 <= idx < len(from_user["cards"])): return await message.answer("У вас нет карты с таким номером.")
    
    card = from_user["cards"].pop(idx)
    to_user["cards"].append(card)
    save_user(str(message.from_user.id), from_user)
    save_user(str(message.reply_to_message.from_user.id), to_user)
    await message.answer(f"🤝 Передал <b>{card['name']}</b> пользователю {message.reply_to_message.from_user.first_name}!", parse_mode="HTML")

@dp.message_handler(commands=['top', 'top_cards', 'stats', 'mystatus'])
async def info_commands(message: types.Message):
    cmd = message.text.split()[0][1:]
    if cmd == 'top':
        res = supabase.table("users").select("name, balance, vip_status").order("balance", desc=True).limit(10).execute()
        text = "🏆 <b>Топ 10 по балансу:</b>\n\n"
        for i, d in enumerate(res.data, 1): text += f"{i}. {get_status_emoji(d)}{d.get('name', 'Игрок')} — <code>{d.get('balance',0)}</code>\n"
        await message.answer(text, parse_mode="HTML")
    elif cmd == 'top_cards':
        res = supabase.table("users").select("name, cards, vip_status").execute()
        top = sorted(res.data, key=lambda x: len(x.get('cards', [])), reverse=True)[:10]
        text = "🏆 <b>Топ 10 по количеству карт:</b>\n\n"
        for i, d in enumerate(top, 1): text += f"{i}. {get_status_emoji(d)}{d.get('name', 'Игрок')} — <code>{len(d.get('cards',[]))}</code> карт\n"
        await message.answer(text, parse_mode="HTML")
    elif cmd == 'stats':
        u = get_user(str(message.from_user.id), message.from_user.first_name)
        st = f"\n✨ Статус: {VIP_STATUSES[u['vip_status']]['name']}" if u.get("vip_status") else ""
        await message.answer(f"📊 <b>Статистика {message.from_user.first_name}</b>\n💰 Баланс: <code>{u['balance']}</code>\n🃏 Карт: <code>{len(u['cards'])}</code>\n⚔️ Уровень дуэли: <b>{u.get('duel_level',0)}</b>{st}", parse_mode="HTML")
    elif cmd == 'mystatus':
        u = get_user(str(message.from_user.id))
        if not u.get("vip_status"): return await message.answer("❌ У тебя нет статуса.")
        s = VIP_STATUSES[u["vip_status"]]
        await message.answer(f"✨ <b>Твой статус:</b> {s['name']}\n💰 Множитель: {s['multiplier']}x\n⏱ Кулдаун: -{s['cd_reduction']} мин", parse_mode="HTML")

# --- ДУЭЛИ ---
@dp.message_handler(commands=['duel'])
async def duel_invite(message: types.Message):
    if not message.reply_to_message: return await message.answer("❌ Ответь на сообщение противника.")
    opp = message.reply_to_message.from_user.id
    if opp == message.from_user.id: return await message.answer("❌ Нельзя вызвать самого себя.")
    if message.from_user.id in duel_requests or opp in duel_requests: return await message.answer("❌ У одного из вас уже есть активный запрос.")

    duel_requests[message.from_user.id] = {"opponent": opp, "timestamp": time.time()}
    await message.answer(f"✅ Вызов отправлен {message.reply_to_message.from_user.first_name}!")
    try:
        await bot.send_message(opp, f"⚔️ {message.from_user.first_name} вызывает вас на дуэль!\nПринять: /accept\nОтклонить: /decline")
    except: pass
    asyncio.create_task(_clear_duel_request(message.from_user.id))

async def _clear_duel_request(cid):
    await asyncio.sleep(DUEL_REQUEST_TIMEOUT)
    duel_requests.pop(cid, None)

@dp.message_handler(commands=['accept'])
async def accept_duel(message: types.Message):
    challenger = next((cid for cid, req in duel_requests.items() if req["opponent"] == message.from_user.id), None)
    if not challenger: return await message.answer("❌ Нет активных приглашений.")
    del duel_requests[challenger]

    c_data, o_data = get_user(str(challenger)), get_user(str(message.from_user.id))
    if not c_data.get("cards") or not o_data.get("cards"): return await message.answer("❌ У одного из вас нет карт.")

    global duel_counter
    did = duel_counter
    duel_counter += 1
    active_duels[did] = {"p1": challenger, "p2": message.from_user.id, "p1_card": None, "p2_card": None, "status": "waiting_choice", "task": None}
    
    await bot.send_message(challenger, f"⚔️ Вызов принят! Выберите карту: /choose <номер> ({DUEL_CHOICE_TIMEOUT} сек.)")
    await message.answer(f"⚔️ Вы приняли вызов! Выберите карту: /choose <номер>")
    active_duels[did]["task"] = asyncio.create_task(_start_choice_timer(did, message.chat.id))

async def _start_choice_timer(did, chat_id):
    await asyncio.sleep(DUEL_CHOICE_TIMEOUT)
    duel = active_duels.get(did)
    if not duel or duel["status"] != "waiting_choice": return

    for role in ("p1", "p2"):
        if duel[f"{role}_card"] is None:
            u = get_user(str(duel[role]))
            duel[f"{role}_card"] = u["cards"][0]["name"] if u.get("cards") else None
            if not duel[f"{role}_card"]: duel["status"] = "timeout"
            
    if duel["status"] == "timeout" or (duel["p1_card"] is None and duel["p2_card"] is None):
        await bot.send_message(chat_id, "⏰ Дуэль отменена.")
        return active_duels.pop(did, None)
    await _resolve_duel(did, chat_id)

async def _resolve_duel(did, chat_id):
    duel = active_duels.get(did)
    if not duel: return
    p1, p2 = get_user(str(duel["p1"])), get_user(str(duel["p2"]))
    
    chance_p1 = get_duel_win_chance(p1.get("duel_level",0), p2.get("duel_level",0))
    winner, loser = (duel["p1"], duel["p2"]) if random.randint(1,100) <= chance_p1 else (duel["p2"], duel["p1"])
    l_card_name = duel["p2_card"] if loser == duel["p2"] else duel["p1_card"]

    l_data, w_data = get_user(str(loser)), get_user(str(winner))
    for i, card in enumerate(l_data["cards"]):
        if card["name"] == l_card_name:
            l_data["cards"].pop(i)
            w_data["cards"].append(card)
            break
            
    save_user(str(loser), l_data)
    save_user(str(winner), w_data)
    
    w_name = (await bot.get_chat(winner)).first_name
    l_name = (await bot.get_chat(loser)).first_name
    await bot.send_message(chat_id, f"⚔️ <b>Результат дуэли</b>\n{w_name} VS {l_name}\nПобедитель: {w_name}\n🏆 {w_name} забирает <b>{l_card_name}</b>!", parse_mode="HTML")
    active_duels.pop(did, None)

@dp.message_handler(commands=['choose'])
async def choose_card(message: types.Message):
    args = message.get_args()
    if not args or not args.isdigit(): return await message.answer("❌ Пример: /choose 3")
    idx = int(args) - 1
    uid = message.from_user.id

    did = next((d for d, v in active_duels.items() if v["status"] == "waiting_choice" and uid in (v["p1"], v["p2"])), None)
    if did is None: return await message.answer("❌ Нет активной дуэли.")
    
    duel = active_duels[did]
    role = "p1" if duel["p1"] == uid else "p2"
    if duel[f"{role}_card"] is not None: return await message.answer("❌ Вы уже выбрали карту.")

    user = get_user(str(uid))
    if not (0 <= idx < len(user.get("cards", []))): return await message.answer("❌ Карты с таким номером нет.")
    
    duel[f"{role}_card"] = user["cards"][idx]["name"]
    await message.answer(f"✅ Выбрана карта <b>{user['cards'][idx]['name']}</b>.", parse_mode="HTML")

    if duel["p1_card"] and duel["p2_card"]:
        if duel["task"]: duel["task"].cancel()
        await _resolve_duel(did, message.chat.id)

@dp.message_handler(commands=['decline'])
async def decline_duel(message: types.Message):
    c = next((cid for cid, req in duel_requests.items() if req["opponent"] == message.from_user.id), None)
    if c:
        del duel_requests[c]
        await message.answer("✅ Вы отклонили вызов.")
        try: await bot.send_message(c, f"❌ {message.from_user.first_name} отклонил вызов.")
        except: pass

@dp.message_handler(commands=['upgrade'])
async def upgrade_duel_level(message: types.Message):
    user = get_user(str(message.from_user.id), message.from_user.first_name)
    if user.get("duel_level", 0) >= 10: return await message.answer("❌ Максимальный уровень (10).")
    price = DUEL_LEVEL_PRICES[user["duel_level"] + 1]
    if user["balance"] < price: return await message.answer(f"❌ Нужно {price} коинов.")
    
    user["balance"] -= price
    user["duel_level"] += 1
    save_user(str(message.from_user.id), user)
    await message.answer(f"✅ Уровень повышен до {user['duel_level']}! Шанс +10%.")

# --- ЗАКАЗЫ И ПРОМОКОДЫ ---
@dp.message_handler(commands=['promo'])
async def use_promo(message: types.Message):
    code = message.get_args().strip()
    if not code: return await message.answer("❌ /promo <код>")
    res = supabase.table("promos").select("*").eq("code", code).execute()
    if not res.data: return await message.answer("❌ Неверный промокод.")
    p = res.data[0]
    if p["uses"] >= p["max_uses"]: return await message.answer("❌ Промокод исчерпан.")

    u = get_user(str(message.from_user.id), message.from_user.first_name)
    u["vip_status"] = p["status"]
    save_user(str(message.from_user.id), u)

    new_uses = p["uses"] + 1
    if new_uses >= p["max_uses"]: supabase.table("promos").delete().eq("code", code).execute()
    else: supabase.table("promos").update({"uses": new_uses}).eq("code", code).execute()
    await message.answer(f"✅ Промокод активирован! Статус: {VIP_STATUSES[p['status']]['name']}.", parse_mode="HTML")

@dp.message_handler(commands=['orders'])
async def list_orders(message: types.Message):
    orders = supabase.table("orders").select("*").eq("status", "active").is_("completed_by", None).execute().data
    if not orders: return await message.answer("📭 Нет активных заказов.")
    text = "📜 <b>Активные заказы:</b>\n\n"
    kb = InlineKeyboardMarkup(row_width=1)
    for idx, o in enumerate(orders, 1):
        rew = f"💰 {o['reward_value']}" if o["reward_type"] == "coins" else f"✨ {VIP_STATUSES.get(o['reward_value'],{}).get('name','')}"
        text += f"{idx}. <b>{o['title']}</b>\n   {o['description']}\n   Награда: {rew}\n\n"
        kb.add(InlineKeyboardButton(f"{idx}. {o['title']}", callback_data=f"order_{o['id']}"))
    await message.answer(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("order_"))
async def order_cb(callback: types.CallbackQuery):
    oid = callback.data.split("_")[1]
    o = supabase.table("orders").select("*").eq("id", oid).execute().data
    if not o or o[0].get("completed_by"): return await callback.answer("Заказ недоступен.", show_alert=True)
    await dp.current_state(user=callback.from_user.id).update_data(order_id=oid)
    await dp.current_state(user=callback.from_user.id).set_state(OrderExecution.waiting_photo)
    await callback.message.answer(f"📸 Отправьте фото для выполнения <b>{o[0]['title']}</b> (или /cancel).", parse_mode="HTML")

@dp.message_handler(state=OrderExecution.waiting_photo, content_types=types.ContentTypes.ANY)
async def exec_order_photo(message: types.Message, state: FSMContext):
    if not message.photo:
        if message.text == '/cancel': await state.finish(); await message.answer("✅ Отменено.")
        else: await message.answer("❌ Нужно ФОТО.")
        return
    data = await state.get_data()
    o = supabase.table("orders").select("*").eq("id", data["order_id"]).execute().data[0]
    
    cap = f"🆕 <b>Заявка</b>\nЗаказ: {o['title']}\nИгрок: ID {message.from_user.id}"
    kb = InlineKeyboardMarkup().add(InlineKeyboardButton("✅", callback_data=f"ac_{o['id']}_{message.from_user.id}"), InlineKeyboardButton("❌", callback_data=f"de_{o['id']}_{message.from_user.id}"))
    for adm in ADMINS:
        try: await bot.send_photo(adm, message.photo[-1].file_id, caption=cap, parse_mode="HTML", reply_markup=kb)
        except: pass
    await message.answer("✅ Фото отправлено админам.")
    await state.finish()

@dp.callback_query_handler(lambda c: c.data and (c.data.startswith("ac_") or c.data.startswith("de_")))
async def process_order_review(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS: return
    action, oid, uid = callback.data.split("_")
    o = supabase.table("orders").select("*").eq("id", oid).execute().data
    if not o or o[0].get("completed_by"): return await callback.message.edit_text("Заказ уже закрыт.")

    if action == "ac":
        supabase.table("orders").update({"status": "completed", "completed_by": uid, "completion_time": time.time()}).eq("id", oid).execute()
        u = get_user(uid)
        if o[0]["reward_type"] == "coins": u["balance"] += int(o[0]["reward_value"])
        else: u["vip_status"] = o[0]["reward_value"]
        save_user(uid, u)
        try: await bot.send_message(uid, f"🎉 Заказ {o[0]['title']} принят!")
        except: pass
        await callback.message.edit_text("✅ Заказ подтвержден.")
    else:
        try: await bot.send_message(uid, f"❌ Заказ {o[0]['title']} отклонен.")
        except: pass
        await callback.message.edit_text("❌ Заказ отклонен.")

# --- ДОБАВЛЕНИЕ КАРТ (SUPABASE FILE ID) ---
@dp.message_handler(commands=['add'], chat_type="private")
@admin_only
async def add_start(message: types.Message):
    await message.answer("Название:"); await AddCard.name.set()

@dp.message_handler(state=AddCard.name)
async def add_name(message: types.Message, state: FSMContext):
    if supabase.table("cards").select("name").eq("name", message.text).execute().data: return await message.answer("Уже есть.")
    await state.update_data(name=message.text); await message.answer("Редкость:"); await AddCard.rarity.set()

@dp.message_handler(state=AddCard.rarity)
async def add_rarity(message: types.Message, state: FSMContext):
    r = message.text.lower()
    if r not in RARITIES: return await message.answer("Неверно.")
    await state.update_data(rarity=r); await message.answer("Цена:"); await AddCard.price.set()

@dp.message_handler(state=AddCard.price)
async def add_price(message: types.Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("Число.")
    await state.update_data(price=int(message.text)); await message.answer("Фото:"); await AddCard.photo.set()

@dp.message_handler(content_types=['photo'], state=AddCard.photo)
async def add_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    supabase.table("cards").insert({"name": data['name'], "rarity": data['rarity'], "price": data['price'], "image": message.photo[-1].file_id}).execute()
    await message.answer(f"✅ Добавлено!")
    await state.finish()

# --- КНОПКИ ---
@dp.message_handler(lambda msg: msg.text in ["🎲 Тпозер", "📦 Инвентарь", "📊 Статистика", "🏆 Топ", "🏪 Рынок", "💫 Статусы", "⚔️ Дуэль", "📜 Заказы"])
async def handle_menu(m: types.Message):
    if m.text == "🎲 Тпозер": await roll_card(m)
    elif m.text == "📦 Инвентарь": await show_inventory(m)
    elif m.text == "📊 Статистика": await info_commands(m)
    elif m.text == "🏆 Топ": m.text = "/top"; await info_commands(m)
    elif m.text == "🏪 Рынок": await show_market(m)
    elif m.text == "💫 Статусы": await m.answer("👑 VIP (1.5x)\n💎 Premium (1.7x)\n🌟 Platinum (1.9x)\n/mystatus")
    elif m.text == "⚔️ Дуэль": await m.answer("/duel (ответом)\n/accept /decline\n/choose\n/upgrade")
    elif m.text == "📜 Заказы": await list_orders(m)

# --- WEB SERVER ДЛЯ ХОСТИНГА ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive and running!"

def run_server():
    app.run(host="0.0.0.0", port=8000)

def keep_alive():
    t = Thread(target=run_server)
    t.start()
    
if __name__ == '__main__':
    keep_alive() # Запускаем веб-сервер
    executor.start_polling(dp, skip_updates=True) # Запускаем бота
