import io
import random
import sqlite3
import asyncio
import logging
import time
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.utils import escape_markdown
from PIL import Image

# Protection against decompression bombs
Image.MAX_IMAGE_PIXELS = 20000000 

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- CONFIG ---
API_ID = "34830678"
API_HASH = "da4f22a0f024f9d17060c3ae45c483be"
BOT_TOKEN = ""

app = Client("puzzle_v14_final", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- DATABASE LOGIC ---
def get_db():
    return sqlite3.connect("economy.db", check_same_thread=False, timeout=10)

def init_db():
    with get_db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS players (user_id INTEGER PRIMARY KEY, points INTEGER DEFAULT 0, name TEXT)")

def add_points(user_id, name, amount):
    try:
        with get_db() as conn:
            conn.execute("INSERT OR IGNORE INTO players (user_id, points, name) VALUES (?, 0, ?)", (user_id, name))
            conn.execute("UPDATE players SET points = points + ?, name = ? WHERE user_id = ?", (amount, name, user_id))
    except Exception as e:
        logger.error(f"DB Error: {e}")

init_db()

games_lock = asyncio.Lock()
games = {} 

# --- HELPERS ---

async def perform_cleanup(game_key):
    """Internal helper to aggressively clear memory."""
    async with games_lock:
        game = games.pop(game_key, None)
        if game:
            if "tiles" in game:
                game["tiles"].clear() # Aggressive cleanup (Polish 1)
            logger.info(f"Memory Cleared: Session {game_key}")

async def auto_cleanup_task(game_key):
    try:
        await asyncio.sleep(900) # 15 mins
        await perform_cleanup(game_key)
    except asyncio.CancelledError:
        pass 

def is_solvable(grid, size):
    arr = [x for x in grid if x is not None]
    inv = sum(1 for i in range(len(arr)) for j in range(i + 1, len(arr)) if arr[i] > arr[j])
    if size % 2 != 0:
        return inv % 2 == 0
    else:
        blank_row = size - (grid.index(None) // size)
        return (inv + blank_row) % 2 != 0

def get_shuffled_grid(size):
    grid = list(range(1, size*size)) + [None]
    while True:
        random.shuffle(grid)
        if is_solvable(grid, size): return grid

def create_puzzle_image(tiles, grid, size):
    canvas_size = 360
    tile_size = canvas_size // size
    new_img = Image.new("RGB", (canvas_size, canvas_size), (25, 25, 25))
    for i, val in enumerate(grid):
        if val is None: continue
        tile = tiles[val - 1]
        row, col = divmod(i, size)
        new_img.paste(tile, (col * tile_size, row * tile_size))
    bio = io.BytesIO()
    new_img.save(bio, format='JPEG', quality=80)
    bio.seek(0)
    return bio

async def safe_edit(client, chat_id, message_id, media, reply_markup):
    for _ in range(5): 
        try:
            return await client.edit_message_media(chat_id=chat_id, message_id=message_id, media=media, reply_markup=reply_markup)
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except MessageNotModified:
            return
        except Exception as e:
            logger.error(f"Safe Edit Error: {e}")
            return

# --- HANDLERS ---

@app.on_message(filters.photo)
async def init_game(client, message):
    chat_id, user_id = message.chat.id, message.from_user.id
    if message.photo.file_size > 10 * 1024 * 1024:
        return await message.reply("❌ Image too large (Max 10MB).")

    async with games_lock:
        if (chat_id, user_id) in games:
            return await message.reply("❌ Pehle purana puzzle solve karein!")

    status = await message.reply("⏳ Cutting tiles...")
    cleanup_task = None 
    
    try:
        photo = await client.download_media(message.photo.file_id, in_memory=True)
        img_bytes = photo.read()
        size = 3 
        
        main_img = Image.open(io.BytesIO(img_bytes)).convert("RGB").resize((360, 360), Image.Resampling.LANCZOS)
        tile_w = 360 // size
        tiles = [main_img.crop((c*tile_w, r*tile_w, (c+1)*tile_w, (r+1)*tile_w)).copy() for r in range(size) for c in range(size)]
        
        # Polish 2: original image close after extraction
        main_img.close() 
        
        grid = get_shuffled_grid(size)
        puzzle_img = create_puzzle_image(tiles, grid, size)
        
        game_key = (chat_id, user_id)
        cleanup_task = asyncio.create_task(auto_cleanup_task(game_key))
        
        async with games_lock:
            games[game_key] = {
                "grid": grid, "tiles": tiles, "orig_bytes": img_bytes,
                "moves": 0, "size": size, "task": cleanup_task,
                "lock": asyncio.Lock(),
                "solved": list(range(1, size*size)) + [None],
                "start_time": time.time()
            }
        
        buttons = [[InlineKeyboardButton(str(grid[r*size+c]) if grid[r*size+c] else "⬜", 
                    callback_data=f"mv_{user_id}_{r*size+c}") for c in range(size)] for r in range(size)]
        
        try: await status.delete()
        except: pass
        
        await message.reply_photo(
            photo=puzzle_img, 
            caption=f"🧩 **Level:** {size}x{size}\n🏅 **Reward:** 100 pts\n🔢 **Moves:** 0", 
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        
    except Exception as e:
        logger.error(f"Init Error: {e}")
        if cleanup_task: cleanup_task.cancel()
        await perform_cleanup((chat_id, user_id))
        try: await status.edit("❌ Processing failed.")
        except: pass

@app.on_callback_query(filters.regex(r"^mv_(\d+)_(\d+)$"))
async def handle_move(client, callback_query):
    parts = callback_query.data.split("_")
    owner_id, idx = int(parts[1]), int(parts[2])
    chat_id = callback_query.message.chat.id
    
    if callback_query.from_user.id != owner_id:
        try: await callback_query.answer("🛑 Not your game!", show_alert=True)
        except: pass
        return
    
    async with games_lock:
        game = games.get((chat_id, owner_id))
    
    if not game:
        try: await callback_query.answer("⚠️ Session Expired!", show_alert=True)
        except: pass
        return

    async with game["lock"]:
        grid, size = game["grid"], game["size"]
        empty_idx = grid.index(None)
        
        if abs(idx // size - empty_idx // size) + abs(idx % size - empty_idx % size) == 1:
            try: await callback_query.answer()
            except: pass
            
            grid[empty_idx], grid[idx] = grid[idx], grid[empty_idx]
            game["moves"] += 1
            curr_time = int(time.time() - game["start_time"])
            
            if grid == game["solved"]:
                game["task"].cancel() 
                add_points(owner_id, callback_query.from_user.first_name, 100)
                
                win_bio = io.BytesIO(game["orig_bytes"])
                win_bio.seek(0)
                
                await safe_edit(client, chat_id, callback_query.message.id, 
                                InputMediaPhoto(win_bio, caption=f"🏆 **SOLVED!**\n💰 +100 Points\n⏱ Time: `{curr_time}s`\n🔢 Moves: `{game['moves']}`"), None)
                
                await perform_cleanup((chat_id, owner_id))
                return

            new_img = create_puzzle_image(game["tiles"], grid, size)
            buttons = [[InlineKeyboardButton(str(grid[r*size+c]) if grid[r*size+c] else "⬜", 
                        callback_data=f"mv_{owner_id}_{r*size+c}") for c in range(size)] for r in range(size)]
            
            await safe_edit(client, chat_id, callback_query.message.id, 
                            InputMediaPhoto(new_img, caption=f"🔢 **Moves:** {game['moves']}\n⏱ **Time:** {curr_time}s"), InlineKeyboardMarkup(buttons))
        else:
            try: await callback_query.answer("Invalid Move! ❌", show_alert=False)
            except: pass

@app.on_message(filters.command("points"))
async def check_points(client, message):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT points FROM players WHERE user_id = ?", (message.from_user.id,))
        row = cursor.fetchone()
    pts = row[0] if row else 0
    await message.reply(f"👤 {message.from_user.first_name}, points: `{pts}`")

@app.on_message(filters.command("top"))
async def leaderboard(client, message):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name, points FROM players ORDER BY points DESC, user_id ASC LIMIT 10")
        rows = cursor.fetchall()
    
    if not rows: return await message.reply("No data yet.")
    
    text = "🏆 **Global Leaderboard**\n\n"
    for i, (name, points) in enumerate(rows, 1):
        text += f"{i}. {escape_markdown(name or 'User')} — `{points} pts`\n"
    await message.reply(text)

app.run()
