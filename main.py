import os
import discord
from discord.ext import commands, tasks
import aiohttp
from aiohttp import web 
import motor.motor_asyncio
import datetime
import asyncio
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# ⚙️ الإعدادات والمتغيرات البيئية
# ==========================================
TOKEN = os.getenv("DISCORD_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")
MONGO_URI = os.getenv("MONGO_URI")

BOOTSTRAP_CHANNEL_ID = int(os.getenv("CHANNEL_BOOTSTRAP_ID"))
PENDING_CHANNEL_ID = int(os.getenv("CHANNEL_NEW_TICKETS_ID"))
IN_PROGRESS_CHANNEL_ID = int(os.getenv("CHANNEL_IN_PROGRESS_ID"))
SUSPENDED_CHANNEL_ID = int(os.getenv("CHANNEL_SUSPENDED_ID")) 
DONE_CHANNEL_ID = int(os.getenv("CHANNEL_DONE_ID"))
ARCHIVE_CHANNEL_ID = int(os.getenv("CHANNEL_ARCHIVE_ID"))
ADMIN_ROLE_ID = int(os.getenv("ROLE_ADMIN_ID"))
BOT_ROLE_ID = int(os.getenv("ROLE_BOT_ID"))

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# 🗄️ إعداد MongoDB
mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
db = mongo_client["gestax_system"]
tickets_collection = db["tickets"]

# تنسيقات العرض
RTL = "\u202b"
WIDTH_HACK = "\u2800" * 15 

# ==========================================
# 🎨 قواميس الألوان والـ Labels (ديناميكية)
# ==========================================
TICKET_COLORS = {
    "bug": 0xE74C3C,       # أحمر (مشكلة/علة)
    "feature": 0x2ECC71,   # أخضر (ميزة جديدة)
    "task": 0x3498DB,      # أزرق (مهمة برمجية)
    "design": 0x9B59B6,    # بنفسجي (تصميم)
    "admin": 0xE67E22,     # برتقالي (إداري)
    "default": 0x95A5A6    # رمادي (أخرى)
}

PRIORITY_EMOJIS = {"high": "🔴 عاجلة جداً", "medium": "🟡 متوسطة", "low": "🟢 عادية"}
TYPE_EMOJIS = {"bug": "🐛 علة (Bug)", "feature": "✨ ميزة (Feature)", "task": "🔨 مهمة (Task)", "design": "🎨 تصميم", "admin": "📁 إدارية"}

# ==========================================
# 🌐 خادم الويب (Render Hack)
# ==========================================
async def fake_web_server():
    app = web.Application()
    app.router.add_get('/', lambda request: web.Response(text="Gestax Bot Active 🚀"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 8080)))
    await site.start()

# ==========================================
# 🐙 GitHub API Manager
# ==========================================
class GitHubAPI:
    @staticmethod
    async def create_issue(title: str, body: str, labels: list) -> int:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/issues"
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        payload = {"title": title, "body": body, "labels": labels}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status == 201: return (await resp.json())["number"]
        return None

    @staticmethod
    async def update_issue_labels(issue_num: int, labels: list):
        url = f"https://api.github.com/repos/{GITHUB_REPO}/issues/{issue_num}"
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        async with aiohttp.ClientSession() as session:
            await session.patch(url, json={"labels": labels}, headers=headers)

    @staticmethod
    async def close_issue(issue_num: int):
        url = f"https://api.github.com/repos/{GITHUB_REPO}/issues/{issue_num}"
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        await aiohttp.ClientSession().patch(url, json={"state": "closed"}, headers=headers)

# ==========================================
# 🛠️ دوال مساعدة لبناء الـ Embed
# ==========================================
def build_ticket_embed(ticket_data, guild: discord.Guild):
    color = TICKET_COLORS.get(ticket_data.get('sub_type', 'default'), TICKET_COLORS['default'])
    
    # العناوين الغليظة باستخدام Markdown
    desc = f"# 🎫 تذكرة #{ticket_data['github_issue_num']}\n"
    desc += f"## {ticket_data['title']}\n\n"
    desc += f"**القسم:** {ticket_data['category']} | **النوع:** {TYPE_EMOJIS.get(ticket_data['sub_type'], 'أخرى')} | **الأولوية:** {PRIORITY_EMOJIS.get(ticket_data['priority'], 'غير محدد')}\n"
    desc += "──────────────\n"
    desc += f"### 📝 الوصف:\n{ticket_data['description']}\n\n"
    desc += f"### 🎯 متطلبات الإنهاء (DoD):\n{ticket_data['dod']}\n"
    
    embed = discord.Embed(description=desc, color=color)
    
    # المنشئ (الأساس)
    creator = guild.get_member(ticket_data['creator_id'])
    creator_mention = creator.mention if creator else "غير معروف"
    creator_avatar = creator.avatar.url if creator and creator.avatar else "https://cdn.discordapp.com/embed/avatars/0.png"
    
    embed.add_field(name="👤 منشئ التذكرة", value=creator_mention, inline=True)
    embed.set_thumbnail(url=creator_avatar)

    # المطور المستلم (إن وجد)
    if ticket_data.get('assignee_id'):
        assignee = guild.get_member(ticket_data['assignee_id'])
        if assignee:
            embed.add_field(name="👨‍💻 المطور المسؤول", value=assignee.mention, inline=True)
            embed.set_footer(text=f"Gestax HQ • {ticket_data['status']}{WIDTH_HACK}", icon_url=assignee.avatar.url if assignee.avatar else None)
        else:
            embed.set_footer(text=f"Gestax HQ • {ticket_data['status']}{WIDTH_HACK}")
    else:
        embed.set_footer(text=f"Gestax HQ • {ticket_data['status']}{WIDTH_HACK}")

    # ختم الوقت الحقيقي للإنشاء
    embed.timestamp = ticket_data['created_at'].replace(tzinfo=datetime.timezone.utc)
    
    return embed

# ==========================================
# 📝 المودالات ونظام الإنشاء (Ticket Builder)
# ==========================================
class TicketCreationModal(discord.ui.Modal):
    def __init__(self, category, sub_type, priority):
        super().__init__(title="تفاصيل التذكرة")
        self.category = category
        self.sub_type = sub_type
        self.priority = priority
        
        self.ticket_title = discord.ui.TextInput(label="عنوان المهمة", style=discord.TextStyle.short)
        self.add_item(self.ticket_title)
        self.ticket_desc = discord.ui.TextInput(label="الوصف", style=discord.TextStyle.long)
        self.add_item(self.ticket_desc)
        self.ticket_dod = discord.ui.TextInput(label="متطلبات الإنهاء (DoD)", style=discord.TextStyle.long)
        self.add_item(self.ticket_dod)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        # تحضير الـ Labels لجيتهاب
        github_labels = [self.category, self.sub_type, f"priority:{self.priority}"]
        issue_body = f"**Category:** {self.category}\n**Priority:** {self.priority}\n\n**Description:**\n{self.ticket_desc.value}\n\n**DoD:**\n{self.ticket_dod.value}"
        
        issue_num = await GitHubAPI.create_issue(self.ticket_title.value, issue_body, github_labels)
        if not issue_num:
            return await interaction.followup.send("❌ حدث خطأ أثناء الاتصال بـ GitHub.", ephemeral=True)
            
        ticket_data = {
            "discord_msg_id": None, # سيتم تحديثه
            "github_issue_num": issue_num,
            "creator_id": interaction.user.id,
            "assignee_id": None,
            "status": "PENDING",
            "category": self.category,
            "sub_type": self.sub_type,
            "priority": self.priority,
            "title": self.ticket_title.value,
            "description": self.ticket_desc.value,
            "dod": self.ticket_dod.value,
            "created_at": datetime.datetime.now(datetime.timezone.utc),
            "github_labels": github_labels
        }

        embed = build_ticket_embed(ticket_data, interaction.guild)
        msg = await bot.get_channel(PENDING_CHANNEL_ID).send(embed=embed, view=PersistentTicketOpsView())
        
        ticket_data["discord_msg_id"] = msg.id
        await tickets_collection.insert_one(ticket_data)
        
        # تنظيف رسالة الـ Builder الأصلية إن أمكن
        try: await interaction.message.delete() 
        except: pass
        
        await interaction.followup.send("✅ تم إنشاء التذكرة بنجاح!", ephemeral=True)

class TicketBuilderView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.category = "programming"
        self.sub_type = "task"
        self.priority = "medium"

    @discord.ui.select(placeholder="1️⃣ اختر القسم...", options=[
        discord.SelectOption(label="برمجية (Programming)", value="programming", emoji="💻"),
        discord.SelectOption(label="تصميم (Design)", value="design", emoji="🎨"),
        discord.SelectOption(label="إدارية (Admin)", value="admin", emoji="📁")
    ], custom_id="select_cat")
    async def select_cat(self, interaction, select):
        self.category = select.values[0]
        await interaction.response.defer()

    @discord.ui.select(placeholder="2️⃣ اختر نوع التذكرة...", options=[
        discord.SelectOption(label="علة / مشكلة (Bug)", value="bug", emoji="🐛"),
        discord.SelectOption(label="ميزة جديدة (Feature)", value="feature", emoji="✨"),
        discord.SelectOption(label="مهمة عمل (Task)", value="task", emoji="🔨")
    ], custom_id="select_type")
    async def select_type(self, interaction, select):
        self.sub_type = select.values[0]
        await interaction.response.defer()

    @discord.ui.select(placeholder="3️⃣ حدد الأولوية (Priority)...", options=[
        discord.SelectOption(label="عاجلة (High)", value="high", emoji="🔴"),
        discord.SelectOption(label="متوسطة (Medium)", value="medium", emoji="🟡"),
        discord.SelectOption(label="عادية (Low)", value="low", emoji="🟢")
    ], custom_id="select_prio")
    async def select_prio(self, interaction, select):
        self.priority = select.values[0]
        await interaction.response.defer()

    @discord.ui.button(label="استمرار وكتابة التفاصيل ➡️", style=discord.ButtonStyle.success, row=3)
    async def proceed(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketCreationModal(self.category, self.sub_type, self.priority))

# ==========================================
# 🔘 تعديل الإعدادات والبيانات
# ==========================================
class EditPropertiesSelect(discord.ui.View):
    def __init__(self, ticket_data):
        super().__init__(timeout=120)
        self.ticket_data = ticket_data

    @discord.ui.select(placeholder="تغيير الأولوية...", options=[
        discord.SelectOption(label="عاجلة", value="high", emoji="🔴"),
        discord.SelectOption(label="متوسطة", value="medium", emoji="🟡"),
        discord.SelectOption(label="عادية", value="low", emoji="🟢")
    ])
    async def edit_prio(self, interaction, select):
        self.ticket_data['priority'] = select.values[0]
        await self.save_and_update(interaction)

    @discord.ui.select(placeholder="تغيير النوع...", options=[
        discord.SelectOption(label="علة (Bug)", value="bug", emoji="🐛"),
        discord.SelectOption(label="ميزة (Feature)", value="feature", emoji="✨"),
        discord.SelectOption(label="مهمة (Task)", value="task", emoji="🔨")
    ])
    async def edit_type(self, interaction, select):
        self.ticket_data['sub_type'] = select.values[0]
        await self.save_and_update(interaction)

    async def save_and_update(self, interaction):
        labels = [self.ticket_data['category'], self.ticket_data['sub_type'], f"priority:{self.ticket_data['priority']}"]
        await tickets_collection.update_one({"discord_msg_id": self.ticket_data['discord_msg_id']}, {"$set": {"priority": self.ticket_data['priority'], "sub_type": self.ticket_data['sub_type'], "github_labels": labels}})
        await GitHubAPI.update_issue_labels(self.ticket_data['github_issue_num'], labels)
        
        embed = build_ticket_embed(self.ticket_data, interaction.guild)
        original_msg = await interaction.channel.fetch_message(self.ticket_data['discord_msg_id'])
        await original_msg.edit(embed=embed)
        await interaction.response.send_message("✅ تم تحديث الخصائص في ديسكورد و جيتهاب.", ephemeral=True)

class EditContentModal(discord.ui.Modal):
    def __init__(self, ticket):
        super().__init__(title="تعديل محتوى التذكرة")
        self.ticket = ticket
        self.title_input = discord.ui.TextInput(label="العنوان", default=ticket['title'])
        self.add_item(self.title_input)
        self.desc_input = discord.ui.TextInput(label="الوصف", default=ticket['description'], style=discord.TextStyle.long)
        self.add_item(self.desc_input)
        self.dod_input = discord.ui.TextInput(label="متطلبات الإنهاء", default=ticket['dod'], style=discord.TextStyle.long)
        self.add_item(self.dod_input)

    async def on_submit(self, interaction: discord.Interaction):
        self.ticket['title'] = self.title_input.value
        self.ticket['description'] = self.desc_input.value
        self.ticket['dod'] = self.dod_input.value
        
        await tickets_collection.update_one({"discord_msg_id": self.ticket['discord_msg_id']}, {"$set": {"title": self.ticket['title'], "description": self.ticket['description'], "dod": self.ticket['dod']}})
        embed = build_ticket_embed(self.ticket, interaction.guild)
        await interaction.message.edit(embed=embed)
        await interaction.response.send_message("✅ تم التعديل.", ephemeral=True)

# ==========================================
# 🔘 الأزرار الرئيسية (المهام النشطة)
# ==========================================
class PersistentTicketOpsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="معلومات 🕒", style=discord.ButtonStyle.secondary, custom_id="btn_info")
    async def show_info(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket = await tickets_collection.find_one({"discord_msg_id": interaction.message.id})
        if not ticket: return
        created = ticket['created_at'].strftime("%Y-%m-%d %H:%M:%S")
        await interaction.response.send_message(f"ℹ️ **تفاصيل مخفية:**\n- معرف التذكرة (ID): `{ticket['_id']}`\n- رقم جيتهاب: `#{ticket['github_issue_num']}`\n- تاريخ الإنشاء: `{created}` UTC", ephemeral=True)

    @discord.ui.button(label="إعدادات التذكرة ⚙️", style=discord.ButtonStyle.secondary, custom_id="btn_edit_props")
    async def edit_props(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket = await tickets_collection.find_one({"discord_msg_id": interaction.message.id})
        if not ticket or (interaction.user.id != ticket['creator_id'] and not any(r.id == ADMIN_ROLE_ID for r in interaction.user.roles)):
            return await interaction.response.send_message("❌ غير مصرح لك.", ephemeral=True)
        await interaction.response.send_message("⚙️ قم بتعديل خصائص جيتهاب للتذكرة:", view=EditPropertiesSelect(ticket), ephemeral=True)

    @discord.ui.button(label="تعديل النص 📝", style=discord.ButtonStyle.secondary, custom_id="btn_edit_text")
    async def edit_text(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket = await tickets_collection.find_one({"discord_msg_id": interaction.message.id})
        if not ticket or interaction.user.id != ticket['creator_id']: return await interaction.response.send_message("❌ لست المنشئ.", ephemeral=True)
        await interaction.response.send_modal(EditContentModal(ticket))

    # أضف هنا أزرار التعيين (Assign) والإنهاء (Done) كما في كودك السابق، مع التأكد من استخدام build_ticket_embed عند التحديث.

class InitialTicketBootstrapView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="فتح تذكرة مهام جديدة 🚀", style=discord.ButtonStyle.success, custom_id="btn_trigger_builder")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🛠️ **إعداد التذكرة:** قم باختيار التصنيفات أولاً ثم اضغط استمرار.", view=TicketBuilderView(), ephemeral=True)

# ==========================================
# 🚀 الإقـلاع والأوامر الإدارية
# ==========================================
@bot.event
async def on_ready():
    bot.add_view(InitialTicketBootstrapView())
    bot.add_view(PersistentTicketOpsView())
    bot.loop.create_task(fake_web_server())
    print(f"🔥 النظام المدمّر أونلاين! البوت: {bot.user}")

@bot.command()
@commands.has_permissions(administrator=True)
async def setup_tickets(ctx):
    embed = discord.Embed(title=f"{RTL} 💼 بوابة إدارة المهام الاحترافية", description=f"{RTL}اضغط لفتح تذكرة. يمكنك تحديد الأولوية، القسم، والنوع بربط مباشر مع GitHub.", color=0x2C3E50)
    await ctx.send(embed=embed, view=InitialTicketBootstrapView())
    await ctx.message.delete()

@bot.command()
@commands.has_permissions(administrator=True)
async def nuke_tickets(ctx):
    """أمر خطير: يقوم بمسح جميع بيانات التذاكر من قاعدة البيانات لتنظيف الاختبارات السابقة."""
    await tickets_collection.delete_many({})
    await ctx.send("💥 **تم مسح جميع التذاكر من قاعدة البيانات!** (ملاحظة: أرقام جيتهاب لا تتصفر، للبدء من 1 يجب عمل مستودع جديد).", delete_after=10)
    await ctx.message.delete()

bot.run(TOKEN)