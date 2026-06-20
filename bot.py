import asyncio
import logging
import os
import sqlite3
from datetime import datetime
from dotenv import load_dotenv

# PTB
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)

# Starlette + Uvicorn (для webhook)
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import Response, JSONResponse
import uvicorn

# ---------- ЗАГРУЗКА ПЕРЕМЕННЫХ ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в .env")

WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # должен быть задан для продакшена

# ---------- НАСТРОЙКА ЛОГОВ ----------
logging.basicConfig(level=logging.INFO)

# ---------- БАЗА ДАННЫХ (SQLite) ----------
DB_PATH = "data/bakery.db"

def init_db():
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            phone TEXT,
            address TEXT,
            created_at TEXT
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            description TEXT,
            price REAL,
            category TEXT,
            image_url TEXT,
            is_active INTEGER DEFAULT 1
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            status TEXT,
            total REAL,
            address TEXT,
            created_at TEXT,
            comment TEXT
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER,
            product_id INTEGER,
            quantity INTEGER,
            price REAL
        )
    ''')
    # Добавим тестовый товар, если таблица пуста
    cur.execute('SELECT COUNT(*) FROM products')
    if cur.fetchone()[0] == 0:
        cur.execute('''
            INSERT INTO products (name, description, price, category, is_active)
            VALUES ('Хлеб на закваске 740 г', 'Пшеничный хлеб на живой закваске Massa Madre. Вес 740 г.', 400, 'хлеб', 1)
        ''')
        cur.execute('''
            INSERT INTO products (name, description, price, category, is_active)
            VALUES ('Бородинский хлеб', 'Ржаной хлеб на закваске с солодом и кориандром.', 350, 'хлеб', 1)
        ''')
        cur.execute('''
            INSERT INTO products (name, description, price, category, is_active)
            VALUES ('Хлеб с изюмом', 'Сладкий пшеничный хлеб с изюмом и корицей.', 420, 'хлеб', 1)
        ''')
        cur.execute('''
            INSERT INTO products (name, description, price, category, is_active)
            VALUES ('Зефир яблочный', 'Натуральный зефир из яблочного пюре и агара.', 250, 'десерты', 1)
        ''')
        cur.execute('''
            INSERT INTO products (name, description, price, category, is_active)
            VALUES ('Творожная масса', 'Домашняя творожная масса с ягодами.', 280, 'десерты', 1)
        ''')
    conn.commit()
    conn.close()

# ---------- РАБОТА С БАЗОЙ ----------
def get_db():
    return sqlite3.connect(DB_PATH)

def add_user(user_id, username, full_name):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        INSERT OR IGNORE INTO users (user_id, username, full_name, created_at)
        VALUES (?, ?, ?, ?)
    ''', (user_id, username, full_name, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_products(category=None):
    conn = get_db()
    cur = conn.cursor()
    if category:
        cur.execute('SELECT * FROM products WHERE category=? AND is_active=1', (category,))
    else:
        cur.execute('SELECT * FROM products WHERE is_active=1')
    rows = cur.fetchall()
    conn.close()
    return rows

def get_product(product_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM products WHERE id=?', (product_id,))
    row = cur.fetchone()
    conn.close()
    return row

def create_order(user_id, address, comment, items):
    conn = get_db()
    cur = conn.cursor()
    total = sum(item['price'] * item['quantity'] for item in items)
    cur.execute('''
        INSERT INTO orders (user_id, status, total, address, created_at, comment)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (user_id, 'новый', total, address, datetime.now().isoformat(), comment))
    order_id = cur.lastrowid
    for item in items:
        cur.execute('''
            INSERT INTO order_items (order_id, product_id, quantity, price)
            VALUES (?, ?, ?, ?)
        ''', (order_id, item['product_id'], item['quantity'], item['price']))
    conn.commit()
    conn.close()
    return order_id

def get_user_orders(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC', (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_order_items(order_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        SELECT oi.quantity, oi.price, p.name
        FROM order_items oi
        JOIN products p ON oi.product_id = p.id
        WHERE oi.order_id=?
    ''', (order_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

# ---------- ВРЕМЕННЫЕ ДАННЫЕ (корзины) ----------
user_carts = {}  # user_id -> list of {product_id, quantity, price, name}

# ---------- КЛАВИАТУРЫ ----------
def main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("🍞 Каталог", callback_data="catalog")],
        [InlineKeyboardButton("🧺 Корзина", callback_data="cart")],
        [InlineKeyboardButton("📦 Мои заказы", callback_data="orders")],
        [InlineKeyboardButton("❓ Задать вопрос AI", callback_data="ask_ai")],
        [InlineKeyboardButton("👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton("🛒 Оформить заказ", callback_data="checkout")],
    ]
    return InlineKeyboardMarkup(keyboard)

def products_keyboard(category=None):
    keyboard = []
    products = get_products(category)
    for p in products:
        keyboard.append([InlineKeyboardButton(f"{p[1]} – {p[3]:.0f} ₽", callback_data=f"product_{p[0]}")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(keyboard)

def product_detail_keyboard(product_id, in_cart=False):
    keyboard = [
        [InlineKeyboardButton("➕ В корзину", callback_data=f"add_to_cart_{product_id}")]
    ]
    if in_cart:
        keyboard.append([InlineKeyboardButton("🗑 Убрать из корзины", callback_data=f"remove_from_cart_{product_id}")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_catalog")])
    return InlineKeyboardMarkup(keyboard)

def cart_keyboard():
    keyboard = [
        [InlineKeyboardButton("🧹 Очистить корзину", callback_data="clear_cart")],
        [InlineKeyboardButton("🛒 Оформить заказ", callback_data="checkout")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(keyboard)

# ---------- ОБРАБОТЧИКИ КОМАНД И CALLBACK'ов ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(user.id, user.username, user.full_name)
    text = (
        "🍞 *Добро пожаловать в пекарню Massa Madre!*\n\n"
        "Мы печём хлеб на живой закваске, зефир, творожную массу и многое другое.\n"
        "Что желаете? Нажмите кнопку ниже."
    )
    await update.message.reply_text(text, reply_markup=main_menu_keyboard(), parse_mode="MarkdownV2")

async def back_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🍞 *Главное меню*", reply_markup=main_menu_keyboard(), parse_mode="MarkdownV2")

async def show_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("📋 *Каталог*\nВыберите категорию:", reply_markup=products_keyboard(), parse_mode="MarkdownV2")

async def show_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split("_")[1])
    product = get_product(product_id)
    if not product:
        await query.edit_message_text("Товар не найден")
        return
    cart = user_carts.get(query.from_user.id, [])
    in_cart = any(item['product_id'] == product_id for item in cart)
    text = f"🍞 *{product[1]}*\n\n{product[2]}\n\n💰 *Цена:* {product[3]:.0f} ₽"
    await query.edit_message_text(text, reply_markup=product_detail_keyboard(product_id, in_cart), parse_mode="MarkdownV2")

async def add_to_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split("_")[3])
    product = get_product(product_id)
    if not product:
        await query.edit_message_text("Товар не найден")
        return
    cart = user_carts.get(query.from_user.id, [])
    for item in cart:
        if item['product_id'] == product_id:
            item['quantity'] += 1
            break
    else:
        cart.append({
            'product_id': product_id,
            'quantity': 1,
            'price': product[3],
            'name': product[1]
        })
    user_carts[query.from_user.id] = cart
    await query.answer(f"✅ {product[1]} добавлен в корзину!", show_alert=False)
    text = f"🍞 *{product[1]}*\n\n{product[2]}\n\n💰 *Цена:* {product[3]:.0f} ₽"
    await query.edit_message_text(text, reply_markup=product_detail_keyboard(product_id, True), parse_mode="MarkdownV2")

async def remove_from_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split("_")[3])
    cart = user_carts.get(query.from_user.id, [])
    new_cart = [item for item in cart if item['product_id'] != product_id]
    user_carts[query.from_user.id] = new_cart
    await query.answer("🗑 Товар удалён из корзины", show_alert=False)
    product = get_product(product_id)
    if product:
        text = f"🍞 *{product[1]}*\n\n{product[2]}\n\n💰 *Цена:* {product[3]:.0f} ₽"
        await query.edit_message_text(text, reply_markup=product_detail_keyboard(product_id, False), parse_mode="MarkdownV2")

async def show_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cart = user_carts.get(query.from_user.id, [])
    if not cart:
        await query.edit_message_text("🧺 *Ваша корзина пуста*", reply_markup=main_menu_keyboard(), parse_mode="MarkdownV2")
        return
    total = 0
    lines = []
    for idx, item in enumerate(cart, 1):
        total += item['price'] * item['quantity']
        lines.append(f"{idx}\\. {item['name']} x{item['quantity']} = {item['price'] * item['quantity']:.0f} ₽")
    text = "🧺 *Ваша корзина*\n\n" + "\n".join(lines) + f"\n\n*Итого:* {total:.0f} ₽"
    await query.edit_message_text(text, reply_markup=cart_keyboard(), parse_mode="MarkdownV2")

async def clear_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_carts[query.from_user.id] = []
    await query.edit_message_text("🧺 *Корзина очищена*", reply_markup=main_menu_keyboard(), parse_mode="MarkdownV2")

# ---------- ОФОРМЛЕНИЕ ЗАКАЗА (ConversationHandler) ----------
ADDRESS, COMMENT = range(2)

async def checkout_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cart = user_carts.get(query.from_user.id, [])
    if not cart:
        await query.answer("Ваша корзина пуста!", show_alert=True)
        return ConversationHandler.END
    await query.edit_message_text(
        "📦 *Оформление заказа*\n\nПожалуйста, введите адрес доставки:",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="MarkdownV2"
    )
    return ADDRESS

async def get_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['address'] = update.message.text
    await update.message.reply_text(
        "📝 Дополнительный комментарий к заказу (или отправьте 'нет'):",
        reply_markup=ReplyKeyboardRemove()
    )
    return COMMENT

async def get_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comment = update.message.text if update.message.text.lower() != "нет" else ""
    address = context.user_data.get('address', '')
    cart = user_carts.get(update.effective_user.id, [])
    if not cart:
        await update.message.reply_text("Корзина пуста. Начните заново.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
    items = [{'product_id': item['product_id'], 'quantity': item['quantity'], 'price': item['price']} for item in cart]
    order_id = create_order(update.effective_user.id, address, comment, items)
    user_carts[update.effective_user.id] = []
    await update.message.reply_text(
        f"✅ *Заказ №{order_id} оформлен!*\n\n"
        f"Адрес: {address}\n"
        f"Комментарий: {comment or 'нет'}\n"
        f"Скоро с вами свяжется менеджер.",
        reply_markup=main_menu_keyboard(),
        parse_mode="MarkdownV2"
    )
    return ConversationHandler.END

async def cancel_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Оформление заказа отменено.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

# ---------- МОИ ЗАКАЗЫ ----------
async def show_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    orders = get_user_orders(query.from_user.id)
    if not orders:
        await query.edit_message_text("📦 *У вас пока нет заказов*", reply_markup=main_menu_keyboard(), parse_mode="MarkdownV2")
        return
    text = "📦 *Ваши заказы:*\n\n"
    for order in orders[:5]:
        order_id, _, status, total, address, created, comment = order
        items = get_order_items(order_id)
        item_lines = "\n".join([f"- {qty}x {name} ({price:.0f} ₽)" for qty, price, name in items])
        text += (
            f"*№{order_id}* от {created[:10]}\n"
            f"Статус: {status}\n"
            f"Сумма: {total:.0f} ₽\n"
            f"Товары:\n{item_lines}\n"
            f"Адрес: {address}\n"
            f"Комментарий: {comment or 'нет'}\n\n"
        )
    await query.edit_message_text(text, reply_markup=main_menu_keyboard(), parse_mode="MarkdownV2")

# ---------- ПРОФИЛЬ ----------
async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT username, full_name, phone, address, created_at FROM users WHERE user_id=?', (query.from_user.id,))
    row = cur.fetchone()
    conn.close()
    if row:
        username, full_name, phone, address, created = row
        text = (
            "👤 *Ваш профиль*\n\n"
            f"Имя: {full_name}\n"
            f"Ник: @{username or 'не указан'}\n"
            f"Телефон: {phone or 'не указан'}\n"
            f"Адрес: {address or 'не указан'}\n"
            f"Зарегистрирован: {created[:10] if created else 'неизвестно'}"
        )
        await query.edit_message_text(text, reply_markup=main_menu_keyboard(), parse_mode="MarkdownV2")
    else:
        await query.edit_message_text("👤 Профиль не найден", reply_markup=main_menu_keyboard())

# ---------- AI-КОНСУЛЬТАНТ (заглушка) ----------
async def ask_ai_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🤖 *Задайте ваш вопрос о нашей продукции,*\n"
        "например: «Какой хлеб подойдёт для бутербродов?» или «Расскажи про зефир»"
    )
    # В этой версии AI не реализован, просто показываем сообщение
    # Можно позже добавить полноценный диалог через ConversationHandler
    await query.edit_message_text(
        "🤖 *AI-консультант пока в разработке.*\n"
        "Пожалуйста, задайте ваш вопрос менеджеру вручную.",
        reply_markup=main_menu_keyboard(),
        parse_mode="MarkdownV2"
    )

# ---------- СОЗДАНИЕ ПРИЛОЖЕНИЯ ----------
def create_application():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(back_main, pattern="^back_main$"))
    application.add_handler(CallbackQueryHandler(show_catalog, pattern="^catalog$"))
    application.add_handler(CallbackQueryHandler(show_product, pattern="^product_"))
    application.add_handler(CallbackQueryHandler(add_to_cart, pattern="^add_to_cart_"))
    application.add_handler(CallbackQueryHandler(remove_from_cart, pattern="^remove_from_cart_"))
    application.add_handler(CallbackQueryHandler(show_cart, pattern="^cart$"))
    application.add_handler(CallbackQueryHandler(clear_cart, pattern="^clear_cart$"))
    application.add_handler(CallbackQueryHandler(show_orders, pattern="^orders$"))
    application.add_handler(CallbackQueryHandler(show_profile, pattern="^profile$"))
    application.add_handler(CallbackQueryHandler(ask_ai_start, pattern="^ask_ai$"))

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(checkout_start, pattern="^checkout$")],
        states={
            ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_address)],
            COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_comment)],
        },
        fallbacks=[CommandHandler("cancel", cancel_checkout)],
    )
    application.add_handler(conv_handler)

    return application

# ---------- ЗАПУСК ----------
async def async_main():
    init_db()
    application = create_application()

    # Обязательная инициализация приложения
    await application.initialize()
    await application.start()

    if WEBHOOK_URL:
        webhook_url = f"{WEBHOOK_URL.rstrip('/')}{WEBHOOK_PATH}"
        await application.bot.set_webhook(webhook_url)
        logging.info(f"Webhook установлен на {webhook_url}")

        async def webhook(request: Request):
            try:
                update_data = await request.json()
                update = Update.de_json(update_data, application.bot)
                await application.process_update(update)
                return Response("OK", status_code=200)
            except Exception as e:
                logging.error(f"Ошибка вебхука: {e}")
                return Response("error", status_code=500)

        starlette_app = Starlette(routes=[
            Route(WEBHOOK_PATH, webhook, methods=["POST"]),
            Route("/", lambda request: JSONResponse({"status": "ok"})),
        ])

        port = int(os.getenv("PORT", 10000))
        config = uvicorn.Config(starlette_app, host="0.0.0.0", port=port, log_level="info")
        server = uvicorn.Server(config)
        await server.serve()
    else:
        logging.warning("WEBHOOK_URL не задан, запуск в режиме polling")
        await application.updater.start_polling()
        await application.updater.idle()

if __name__ == "__main__":
    asyncio.run(async_main())
from telegram_escape import tg_escape

# В функции start:
safe_text = tg_escape(text)
await update.message.reply_text(safe_text, reply_markup=main_menu_keyboard(), parse_mode="MarkdownV2")
