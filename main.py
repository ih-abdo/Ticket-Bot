import os
import discord
from discord.ext import commands, tasks
import aiohttp
from aiohttp import web 
import motor.motor_asyncio
import datetime
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")
MONGO_URI = os.getenv("MONGO_URI")

# --- إعدادات القنوات ---
BOOTSTRAP_CHANNEL_ID = int(os.getenv("CHANNEL_BOOTSTRAP_ID"))
PENDING_CHANNEL_ID = int(os.getenv("CHANNEL_NEW_TICKETS_ID"))
IN_PROGRESS_CHANNEL_ID = int(os.getenv("CHANNEL_IN_PROGRESS_ID"))
SUSPENDED_CHANNEL_ID = int(os.getenv("CHANNEL_SUSPENDED_ID")) 
DONE_CHANNEL_ID = int(os.getenv("CHANNEL_DONE_ID"))
ARCHIVE_CHANNEL_ID = int(os.getenv("CHANNEL_ARCHIVE_ID"))
LOG_CHANNEL_ID = int(os.getenv("CHANNEL_LOG_ID")) 

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# --------------------------------------------------------
# 🗄️ إعداد MongoDB
# --------------------------------------------------------
mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
db = mongo_client["gestax_system"]
tickets_collection = db["tickets"]
configs_collection = db["server_configs"] 

RTL = "\u202b"
WIDTH_HACK = "\u2800" * 45  

# --------------------------------------------------------
# 🛡️ نظام الصلاحيات شديد الدقة (Granular Permissions)
# --------------------------------------------------------
PERM_NAMES = {
    "create_task": "📝 فتح تذاكر المهام",
    "create_bug": "🐞 الإبلاغ عن الأخطاء",
    "assign_ticket": "👤 تعيين المطورين",
    "suspend_ticket": "⏸️ تعليق التذاكر",
    "unsuspend_ticket": "▶️ إلغاء التعليق",
    "force_close": "❌ الحذف والإغلاق الإجباري",
    "archive_ticket": "📁 الأرشفة اليدوية"
}

async def get_guild_config(guild_id: int):
    config = await configs_collection.find_one({"guild_id": guild_id})
    if not config:
        config = {"guild_id": guild_id, "perms": {k: [] for k in PERM_NAMES.keys()}}
        await configs_collection.insert_one(config)
    return config

async def check_permission(guild_id: int, user: discord.Member, perm_type: str) -> bool:
    if user.guild.owner_id == user.id or user.guild_permissions.administrator:
        return True
    config = await get_guild_config(guild_id)
    allowed_roles = config.get("perms", {}).get(perm_type, [])
    user_role_ids = [role.id for role in user.roles]
    return any(role_id in allowed_roles for role_id in user_role_ids)

# --- لوحة التحكم المتطورة ---
class RoleAssignView(discord.ui.View):
    def __init__(self, guild_id: int, perm_key: str):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.perm_key = perm_key

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="اختر الرتب المسموح لها بهذا الإجراء...", min_values=0, max_values=10)
    async def select_roles(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        role_ids = [role.id for role in select.values]
        await configs_collection.update_one(
            {"guild_id": self.guild_id}, 
            {"$set": {f"perms.{self.perm_key}": role_ids}}, 
            upsert=True
        )
        await interaction.response.send_message(f"✅ تم تحديث صلاحية **{PERM_NAMES[self.perm_key]}** بنجاح.", ephemeral=True)

class DashboardMainSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=name, value=key) for key, name in PERM_NAMES.items()]
        super().__init__(placeholder="🛠️ اختر الصلاحية التي تريد ضبط رتبها...", options=options)

    async def callback(self, interaction: discord.Interaction):
        selected_perm = self.values[0]
        perm_name = PERM_NAMES[selected_perm]
        embed = discord.Embed(
            title=f"ضبط صلاحية: {perm_name}", 
            description="حدد الرتب التي يحق لها القيام بهذا الإجراء من القائمة أدناه.\n*(لحذف كل الرتب، قم بإلغاء التحديد واضغط خارج القائمة)*",
            color=0x3498DB
        )
        await interaction.response.send_message(embed=embed, view=RoleAssignView(interaction.guild.id, selected_perm), ephemeral=True)

class DashboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(DashboardMainSelect())

@bot.command()
@commands.has_permissions(administrator=True)
async def dashboard(ctx):
    """لوحة تحكم الصلاحيات الاحترافية"""
    config = await get_guild_config(ctx.guild.id)
    embed = discord.Embed(
        title="🎛️ مركز تحكم الصلاحيات المتقدم (Gestax Command Center)",
        description="هنا يمكنك تخصيص من يمكنه فعل ماذا بدقة متناهية. الإدارة العليا وصاحب السيرفر لديهم جميع الصلاحيات دائماً.\n\n**الوضع الحالي:**",
        color=0x9B59B6
    )
    for key, name in PERM_NAMES.items():
        roles = config.get("perms", {}).get(key, [])
        role_mentions = " ".join([f"<@&{r}>" for r in roles]) if roles else "لا أحد (الإدارة فقط)"
        embed.add_field(name=name, value=role_mentions, inline=False)

    await ctx.send(embed=embed, view=DashboardView())
    await ctx.message.delete()

# --------------------------------------------------------
# 📜 سجل النظام و GitHub API
# --------------------------------------------------------
async def send_audit_log(guild: discord.Guild, user: discord.Member, action: str, issue_num: int, title: str, color: int, extra: str = ""):
    log_channel = guild.get_channel(LOG_CHANNEL_ID)
    if not log_channel: return
    embed = discord.Embed(title=f"📜 سجل النظام | {action}", color=color, timestamp=datetime.datetime.now(datetime.timezone.utc))
    embed.add_field(name="👤 بواسطة", value=user.mention, inline=True)
    embed.add_field(name="🎫 التذكرة", value=f"#{issue_num} | {title}", inline=True)
    if extra: embed.add_field(name="📝 التفاصيل", value=extra, inline=False)
    embed.set_footer(text=f"Gestax Security", icon_url=user.display_avatar.url)
    await log_channel.send(embed=embed)

async def fake_web_server():
    app = web.Application()
    app.router.add_get('/', lambda request: web.Response(text="Gestax Discord Bot is Alive! 🚀"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

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
    async def close_issue(issue_num: int) -> bool:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/issues/{issue_num}"
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        payload = {"state": "closed"}
        async with aiohttp.ClientSession() as session:
            async with session.patch(url, json=payload, headers=headers) as resp:
                return resp.status == 200

# ========================================================
# 1️⃣ نظام المهام (التوافق التام: ديساكورد عربي - جيتهاب إنجليزي)
# ========================================================
class TicketCreationModal(discord.ui.Modal):
    def __init__(self, cat_ar, cat_en, kind_ar, kind_en, urg_ar, urg_en, is_custom):
        super().__init__(title="تفاصيل التذكرة البرمجية")
        self.cat_ar, self.cat_en = cat_ar, cat_en
        self.kind_ar, self.kind_en = kind_ar, kind_en
        self.urg_ar, self.urg_en = urg_ar, urg_en
        self.is_custom = is_custom

        self.ticket_title = discord.ui.TextInput(label="عنوان المهمة", placeholder="مثال: إضافة نظام الدفع...")
        self.add_item(self.ticket_title)

        if self.is_custom:
            self.custom_type = discord.ui.TextInput(label="تصنيف القسم (كتابة حرة)", placeholder="مثال: واجهة المستخدم")
            self.add_item(self.custom_type)

        self.ticket_desc = discord.ui.TextInput(label="تفاصيل المهمة المطلوبة", style=discord.TextStyle.long)
        self.add_item(self.ticket_desc)
        self.ticket_dod = discord.ui.TextInput(label="Definition of Done (متى نعتبرها منتهية؟)", style=discord.TextStyle.long)
        self.add_item(self.ticket_dod)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        final_cat_ar = self.custom_type.value.strip() if self.is_custom else self.cat_ar
        final_cat_en = self.custom_type.value.strip() if self.is_custom else self.cat_en

        # جيتهاب يستقبل الإنجليزي فقط
        gh_labels = [final_cat_en, self.kind_en, self.urg_en]
        issue_body = f"Requested by: {interaction.user.name}\n\n**Description:**\n{self.ticket_desc.value}\n\n**DoD:**\n{self.ticket_dod.value}"
        
        issue_num = await GitHubAPI.create_issue(self.ticket_title.value, issue_body, gh_labels)
        if not issue_num:
            await interaction.followup.send("❌ فشل فتح التذكرة في جيتهاب.", ephemeral=True)
            return
        
        formatted_body = f"**{RTL}📋 الوصف:**\n{RTL}{self.ticket_desc.value}\n\n**{RTL}🎯 متطلبات الإنهاء (DoD):**\n{RTL}{self.ticket_dod.value}"
        
        # ديسكورد يعرض العربي فقط
        embed = discord.Embed(
            title=f"{RTL} ⏳ تذكرة #{issue_num} | {self.ticket_title.value}",
            description=f"{RTL}**القسم:** {final_cat_ar} | **النوع:** {self.kind_ar} | **الأولوية:** {self.urg_ar}\n\n{formatted_body}",
            color=0xF1C40F
        )
        embed.set_author(name=f"بواسطة: {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        embed.set_footer(text=f"Gestax HQ • Pending{WIDTH_HACK}", icon_url="https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png")
        
        pending_channel = bot.get_channel(PENDING_CHANNEL_ID)
        msg = await pending_channel.send(embed=embed, view=PersistentTicketOpsView())
        
        await tickets_collection.insert_one({
            "discord_msg_id": msg.id, "github_issue_num": issue_num,
            "creator_id": interaction.user.id, "assignee_id": None, "thread_id": None,
            "status": "PENDING", "category": final_cat_ar, "kind": self.kind_ar, "urgency": self.urg_ar,
            "title": self.ticket_title.value, "formatted_body": formatted_body,
            "created_at": datetime.datetime.now(datetime.timezone.utc)
        })
        await send_audit_log(interaction.guild, interaction.user, "إنشاء تذكرة 📝", issue_num, self.ticket_title.value, 0x3498DB, f"**القسم:** {final_cat_ar}")
        await interaction.followup.send("✅ تم إرسال التذكرة بنجاح.", ephemeral=True)

class TicketConfigView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.cat_data = None
        self.kind_data = None
        self.urg_data = None

    @discord.ui.select(placeholder="1️⃣ القسم (اختر القسم الخاص بالمهمة)...", options=[
        discord.SelectOption(label="برمجية", emoji="💻", value="برمجية|Category: Software"),
        discord.SelectOption(label="تصميم", emoji="🎨", value="تصميم|Category: Design"),
        discord.SelectOption(label="إدارية", emoji="📁", value="إدارية|Category: Management"),
        discord.SelectOption(label="أخرى (كتابة حرة)", emoji="⚙️", value="مخصص|custom")
    ])
    async def select_category(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.cat_data = select.values[0].split("|")
        await interaction.response.defer()

    @discord.ui.select(placeholder="2️⃣ نوع المهمة (تاسك أم فيتشور)...", options=[
        discord.SelectOption(label="ميزة جديدة (Feature)", emoji="✨", value="ميزة (Feature)|Type: Feature"),
        discord.SelectOption(label="مهمة عمل (Task)", emoji="📋", value="مهمة (Task)|Type: Task")
    ])
    async def select_kind(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.kind_data = select.values[0].split("|")
        await interaction.response.defer()

    @discord.ui.select(placeholder="3️⃣ مدى الاستعجال (الأولوية)...", options=[
        discord.SelectOption(label="منخفضة", emoji="🟢", value="منخفضة 🟢|Priority: Low"),
        discord.SelectOption(label="متوسطة", emoji="🟡", value="متوسطة 🟡|Priority: Medium"),
        discord.SelectOption(label="عالية", emoji="🟠", value="عالية 🟠|Priority: High"),
        discord.SelectOption(label="حرجة", emoji="🔴", value="حرجة 🔴|Priority: Critical")
    ])
    async def select_urgency(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.urg_data = select.values[0].split("|")
        await interaction.response.defer()

    @discord.ui.button(label="متابعة وكتابة التفاصيل ➡️", style=discord.ButtonStyle.success, row=3)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await check_permission(interaction.guild.id, interaction.user, "create_task"):
            await interaction.response.send_message("❌ لا تملك الصلاحية اللازمة لفتح تذاكر مهام.", ephemeral=True)
            return
        if not self.cat_data or not self.kind_data or not self.urg_data:
            await interaction.response.send_message("❌ يرجى اختيار القسم، النوع، ومدى الاستعجال أولاً!", ephemeral=True)
            return
            
        is_custom = (self.cat_data[1] == "custom")
        await interaction.response.send_modal(TicketCreationModal(
            self.cat_data[0], self.cat_data[1], 
            self.kind_data[0], self.kind_data[1], 
            self.urg_data[0], self.urg_data[1], 
            is_custom
        ))

class InitialTicketBootstrapView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="فتح تذكرة مهام 📝", style=discord.ButtonStyle.primary, custom_id="btn_open_standard")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await check_permission(interaction.guild.id, interaction.user, "create_task"):
            await interaction.response.send_message("❌ لا تملك الصلاحية.", ephemeral=True)
            return
        await interaction.response.send_message("⚙️ **إعداد التذكرة:** حدد الخيارات التوضيحية للمهمة:", view=TicketConfigView(), ephemeral=True)

# ========================================================
# 2️⃣ نظام البلاغات (Bugs)
# ========================================================
class BugCreationModal(discord.ui.Modal):
    def __init__(self, urg_ar, urg_en):
        super().__init__(title="الإبلاغ عن خلل برمجي (Bug)")
        self.urg_ar = urg_ar
        self.urg_en = urg_en
        
        self.bug_title = discord.ui.TextInput(label="عنوان المشكلة باختصار")
        self.add_item(self.bug_title)
        self.bug_reproduce = discord.ui.TextInput(label="خطوات إعادة الإنتاج", style=discord.TextStyle.long)
        self.add_item(self.bug_reproduce)
        self.bug_expected = discord.ui.TextInput(label="السلوك المتوقع", style=discord.TextStyle.paragraph)
        self.add_item(self.bug_expected)
        self.bug_actual = discord.ui.TextInput(label="السلوك الفعلي/الخطأ", style=discord.TextStyle.paragraph)
        self.add_item(self.bug_actual)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        gh_labels = ["Type: Bug", self.urg_en]
        issue_body = f"Reported by: {interaction.user.name}\n\n**Steps to Reproduce:**\n{self.bug_reproduce.value}\n\n**Expected:**\n{self.bug_expected.value}\n\n**Actual:**\n{self.bug_actual.value}"
        
        issue_num = await GitHubAPI.create_issue(self.bug_title.value, issue_body, gh_labels)
        if not issue_num: return await interaction.followup.send("❌ فشل فتح التذكرة.", ephemeral=True)
        
        formatted_body = f"**{RTL}🔄 خطوات إعادة الإنتاج:**\n{RTL}{self.bug_reproduce.value}\n\n**{RTL}🎯 السلوك المتوقع:**\n{RTL}{self.bug_expected.value}\n\n**{RTL}⚠️ السلوك الفعلي:**\n{RTL}{self.bug_actual.value}"
        
        embed = discord.Embed(
            title=f"{RTL} 🐞 بلاغ خطأ #{issue_num} | {self.bug_title.value}",
            description=f"{RTL}**القسم:** باق (Bug) 🐞 | **الأولوية:** {self.urg_ar}\n\n{formatted_body}",
            color=0xE74C3C
        )
        embed.set_author(name=f"بواسطة: {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        embed.set_footer(text=f"Gestax HQ • Bug Report{WIDTH_HACK}")
        
        pending_channel = bot.get_channel(PENDING_CHANNEL_ID)
        msg = await pending_channel.send(embed=embed, view=PersistentTicketOpsView())
        
        await tickets_collection.insert_one({
            "discord_msg_id": msg.id, "github_issue_num": issue_num,
            "creator_id": interaction.user.id, "assignee_id": None, "thread_id": None,
            "status": "PENDING", "category": "باق (Bug) 🐞", "kind": "Bug", "urgency": self.urg_ar,
            "title": self.bug_title.value, "formatted_body": formatted_body,
            "created_at": datetime.datetime.now(datetime.timezone.utc)
        })
        await send_audit_log(interaction.guild, interaction.user, "بلاغ خطأ 🐞", issue_num, self.bug_title.value, 0xE74C3C)
        await interaction.followup.send("✅ تم الإرسال.", ephemeral=True)

class BugConfigView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.urg_data = None

    @discord.ui.select(placeholder="🚨 اختر مدى خطورة هذا العطل...", options=[
        discord.SelectOption(label="منخفضة", emoji="🟢", value="منخفضة 🟢|Priority: Low"),
        discord.SelectOption(label="متوسطة", emoji="🟡", value="متوسطة 🟡|Priority: Medium"),
        discord.SelectOption(label="عالية", emoji="🟠", value="عالية 🟠|Priority: High"),
        discord.SelectOption(label="حرجة", emoji="🔴", value="حرجة 🔴|Priority: Critical")
    ])
    async def select_urgency(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.urg_data = select.values[0].split("|")
        await interaction.response.defer()

    @discord.ui.button(label="متابعة لكتابة التفاصيل ➡️", style=discord.ButtonStyle.danger, row=1)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await check_permission(interaction.guild.id, interaction.user, "create_bug"):
            await interaction.response.send_message("❌ لا تملك الصلاحية.", ephemeral=True)
            return
        if not self.urg_data:
            await interaction.response.send_message("❌ حدد الخطورة أولاً!", ephemeral=True)
            return
        await interaction.response.send_modal(BugCreationModal(self.urg_data[0], self.urg_data[1]))

class BugBootstrapView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="إبلاغ عن عيب برمجي (Bug) 🐞", style=discord.ButtonStyle.danger, custom_id="btn_open_bug")
    async def open_bug(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await check_permission(interaction.guild.id, interaction.user, "create_bug"):
            await interaction.response.send_message("❌ لا تملك الصلاحية.", ephemeral=True)
            return
        await interaction.response.send_message("⚙️ حدد مستوى الخطورة:", view=BugConfigView(), ephemeral=True)

# ========================================================
# 3️⃣ العمليات المشتركة (تعيين، تعليق، إنهاء، أرشفة)
# ========================================================
class SuspendModal(discord.ui.Modal, title="سبب تعليق التذكرة"):
    reason = discord.ui.TextInput(label="لماذا تريد تعليق العمل؟", style=discord.TextStyle.long)
    def __init__(self, msg_id: int):
        super().__init__()
        self.msg_id = msg_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        ticket = await tickets_collection.find_one({"discord_msg_id": self.msg_id})
        if not ticket: return

        embed = discord.Embed(
            title=f"{RTL} 🛑 معلقة #{ticket['github_issue_num']} | {ticket['title']}", 
            description=f"{RTL}**القسم:** {ticket['category']} | **النوع:** {ticket['kind']} | **الأولوية:** {ticket['urgency']}\n\n{ticket['formatted_body']}\n\n**{RTL}⚠️ سبب الإيقاف:**\n{RTL}{self.reason.value}", 
            color=0xE74C3C
        )
        creator_user = interaction.guild.get_member(ticket['creator_id'])
        embed.set_author(name=f"المنشئ: {creator_user.display_name if creator_user else 'غير معروف'}", icon_url=creator_user.display_avatar.url if creator_user else interaction.user.default_avatar.url)
        if ticket['assignee_id']:
            assignee_user = interaction.guild.get_member(ticket['assignee_id'])
            if assignee_user: embed.add_field(name=f"{RTL}المطور المسؤول", value=assignee_user.mention, inline=False)
        embed.set_footer(text=f"Gestax HQ • Suspended{WIDTH_HACK}")
        
        new_msg = await bot.get_channel(SUSPENDED_CHANNEL_ID).send(embed=embed, view=SuspendedTicketOpsView())
        await tickets_collection.update_one({"discord_msg_id": self.msg_id}, {"$set": {"discord_msg_id": new_msg.id, "status": "SUSPENDED"}})
        
        if ticket['thread_id']:
            try:
                thread = interaction.guild.get_thread(ticket['thread_id'])
                if thread: await thread.send("🛑 **تنبيه:** تم تعليق العمل على هذه المهمة ونقلها للانتظار.")
            except: pass
        await send_audit_log(interaction.guild, interaction.user, "تعليق تذكرة 🛑", ticket['github_issue_num'], ticket['title'], 0xE74C3C, f"**السبب:** {self.reason.value}")
        await interaction.message.delete()
        await interaction.followup.send("⏸️ تم النقل لقناة التعليق.", ephemeral=True)

class AssigneeSelect(discord.ui.UserSelect):
    def __init__(self, msg_id: int):
        super().__init__(custom_id=f"assign_select_menu:{msg_id}", placeholder="اختر المطور...", min_values=1, max_values=1)
        self.msg_id = msg_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        dev = self.values[0]
        if dev.bot: return await interaction.followup.send("❌ لا يمكنك تعيين روبوت!", ephemeral=True)

        ticket = await tickets_collection.find_one({"discord_msg_id": self.msg_id})
        if not ticket: return

        work_embed = discord.Embed(
            title=f"{RTL} 👨‍💻 قيد العمل #{ticket['github_issue_num']} | {ticket['title']}", 
            description=f"{RTL}**القسم:** {ticket['category']} | **النوع:** {ticket['kind']} | **الأولوية:** {ticket['urgency']}\n\n{ticket['formatted_body']}", 
            color=0x3498DB
        )
        creator_user = interaction.guild.get_member(ticket['creator_id'])
        work_embed.set_author(name=f"المنشئ: {creator_user.display_name if creator_user else 'غير معروف'}", icon_url=creator_user.display_avatar.url if creator_user else interaction.user.default_avatar.url)
        work_embed.add_field(name=f"{RTL}المستلم", value=dev.mention, inline=False)
        work_embed.set_footer(text=f"Gestax HQ • In Progress{WIDTH_HACK}")
        
        if ticket['thread_id'] and ticket['status'] in ['IN_PROGRESS', 'SUSPENDED']:
            thread = interaction.guild.get_thread(ticket['thread_id'])
            if thread:
                await thread.add_user(dev)
                await thread.send(f"🔄 تم تسليم التذكرة للمطور {dev.mention}.")
            await interaction.message.edit(embed=work_embed)
            await tickets_collection.update_one({"discord_msg_id": self.msg_id}, {"$set": {"assignee_id": dev.id, "status": "IN_PROGRESS"}})
        else:
            in_progress_channel = bot.get_channel(IN_PROGRESS_CHANNEL_ID)
            new_msg = await in_progress_channel.send(embed=work_embed, view=PersistentTicketOpsView())
            thread = await in_progress_channel.create_thread(name=f"🔒-عمل-{ticket['github_issue_num']}", type=discord.ChannelType.private_thread, auto_archive_duration=1440)
            await thread.add_user(interaction.user)
            if ticket['creator_id']: await thread.add_user(interaction.guild.get_member(ticket['creator_id']))
            await thread.add_user(dev)
            await thread.send(f"⚠️ مساحة عمل سرية لمناقشة #{ticket['github_issue_num']}.")
            await tickets_collection.update_one({"discord_msg_id": self.msg_id}, {"$set": {"discord_msg_id": new_msg.id, "assignee_id": dev.id, "thread_id": thread.id, "status": "IN_PROGRESS"}})
            try: await interaction.message.delete()
            except Exception: pass
            
        await send_audit_log(interaction.guild, interaction.user, "تعيين مطور 👨‍💻", ticket['github_issue_num'], ticket['title'], 0x9B59B6, f"**المستلم:** {dev.mention}")
        await interaction.followup.send("🎯 تم تحديث التذكرة.", ephemeral=True)

class AssigneeSelectView(discord.ui.View):
    def __init__(self, msg_id: int):
        super().__init__(timeout=60)
        self.add_item(AssigneeSelect(msg_id))

class SuspendedTicketOpsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="نزع التعليق 🔙", style=discord.ButtonStyle.primary, custom_id="btn_unsuspend")
    async def unsuspend_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await check_permission(interaction.guild.id, interaction.user, "unsuspend_ticket"):
            return await interaction.response.send_message("❌ لا تملك الصلاحية.", ephemeral=True)
            
        await interaction.response.defer(ephemeral=True)
        ticket = await tickets_collection.find_one({"discord_msg_id": interaction.message.id})
        if not ticket: return

        embed = discord.Embed(
            title=f"{RTL} ⏳ تذكرة #{ticket['github_issue_num']} | {ticket['title']}",
            description=f"{RTL}**القسم:** {ticket['category']} | **النوع:** {ticket['kind']} | **الأولوية:** {ticket['urgency']}\n\n{ticket['formatted_body']}",
            color=0xF1C40F
        )
        creator_user = interaction.guild.get_member(ticket['creator_id'])
        embed.set_author(name=f"المنشئ: {creator_user.display_name if creator_user else 'غير معروف'}", icon_url=creator_user.display_avatar.url if creator_user else interaction.user.default_avatar.url)
        embed.set_footer(text=f"Gestax HQ • Pending{WIDTH_HACK}")
        
        new_msg = await bot.get_channel(PENDING_CHANNEL_ID).send(embed=embed, view=PersistentTicketOpsView())
        if ticket['thread_id']:
            try:
                thread = interaction.guild.get_thread(ticket['thread_id'])
                if thread: await thread.send("▶️ تم نزع التعليق.")
            except: pass

        await tickets_collection.update_one({"discord_msg_id": interaction.message.id}, {"$set": {"discord_msg_id": new_msg.id, "status": "PENDING"}})
        await send_audit_log(interaction.guild, interaction.user, "إلغاء تعليق ▶️", ticket['github_issue_num'], ticket['title'], 0x2ECC71)
        await interaction.message.delete()
        await interaction.followup.send("🔙 عادت للانتظار.", ephemeral=True)

    @discord.ui.button(label="إغلاق إجباري ❌", style=discord.ButtonStyle.danger, custom_id="btn_suspend_force_close")
    async def force_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await check_permission(interaction.guild.id, interaction.user, "force_close"):
            return await interaction.response.send_message("❌ لا تملك الصلاحية.", ephemeral=True)
        await handle_force_close(interaction)

class DoneTicketOpsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="أرشفة التذكرة 📁", style=discord.ButtonStyle.secondary, custom_id="btn_archive_done")
    async def archive_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await check_permission(interaction.guild.id, interaction.user, "archive_ticket"):
            return await interaction.response.send_message("❌ لا تملك الصلاحية.", ephemeral=True)
            
        ticket = await tickets_collection.find_one({"discord_msg_id": interaction.message.id})
        await bot.get_channel(ARCHIVE_CHANNEL_ID).send(embed=interaction.message.embeds[0])
        await tickets_collection.delete_one({"discord_msg_id": interaction.message.id})
        if ticket: await send_audit_log(interaction.guild, interaction.user, "أرشفة يدوية 📁", ticket['github_issue_num'], ticket['title'], 0x95A5A6)
        await interaction.message.delete()
        await interaction.response.send_message("📁 تمت الأرشفة.", ephemeral=True)

class PersistentTicketOpsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="تعيين 👤", style=discord.ButtonStyle.primary, custom_id="btn_global_assign")
    async def assign_dev(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await check_permission(interaction.guild.id, interaction.user, "assign_ticket"):
            return await interaction.response.send_message("❌ لا تملك الصلاحية.", ephemeral=True)
        await interaction.response.send_message("اختر المطور:", view=AssigneeSelectView(interaction.message.id), ephemeral=True)

    @discord.ui.button(label="تعليق ⏸️", style=discord.ButtonStyle.secondary, custom_id="btn_global_suspend")
    async def suspend_task(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await check_permission(interaction.guild.id, interaction.user, "suspend_ticket"):
            return await interaction.response.send_message("❌ لا تملك الصلاحية.", ephemeral=True)
        await interaction.response.send_modal(SuspendModal(interaction.message.id))

    @discord.ui.button(label="إنهاء ✅", style=discord.ButtonStyle.success, custom_id="btn_mark_done")
    async def mark_done(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ticket = await tickets_collection.find_one({"discord_msg_id": interaction.message.id})
        if not ticket: return

        has_force = await check_permission(interaction.guild.id, interaction.user, "force_close")
        if not has_force and interaction.user.id != ticket['assignee_id']:
            return await interaction.followup.send("❌ فقط المطور المسؤول أو الإدارة العليا يمكنهم الإنهاء.", ephemeral=True)

        await GitHubAPI.close_issue(ticket['github_issue_num'])
        done_embed = discord.Embed(
            title=f"{RTL} 🎉 مكتملة #{ticket['github_issue_num']} | {ticket['title']}", 
            description=f"{RTL}**القسم:** {ticket['category']} | **النوع:** {ticket['kind']} | **الأولوية:** {ticket['urgency']}\n\n{ticket['formatted_body']}", 
            color=0x2ECC71
        )
        done_embed.set_footer(text=f"Gestax HQ • Completed • {datetime.datetime.now().strftime('%Y-%m-%d')}{WIDTH_HACK}")
        
        new_msg = await bot.get_channel(DONE_CHANNEL_ID).send(embed=done_embed, view=DoneTicketOpsView())

        if ticket['thread_id']:
            try:
                thread = interaction.guild.get_thread(ticket['thread_id'])
                if thread:
                    await thread.send("🔒 **مكتمل:** تم إنهاء المهمة وقفل الغرفة.")
                    await thread.edit(archived=True, locked=True)
            except Exception: pass

        await tickets_collection.update_one({"discord_msg_id": interaction.message.id}, {"$set": {"discord_msg_id": new_msg.id, "status": "DONE"}})
        await send_audit_log(interaction.guild, interaction.user, "إنهاء واعتماد ✅", ticket['github_issue_num'], ticket['title'], 0x2ECC71)
        await interaction.message.delete()
        await interaction.followup.send("✅ اكتملت المهمة.", ephemeral=True)

    @discord.ui.button(label="إغلاق ❌", style=discord.ButtonStyle.danger, custom_id="btn_global_force_close")
    async def force_close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await check_permission(interaction.guild.id, interaction.user, "force_close"):
            return await interaction.response.send_message("❌ لا تملك الصلاحية.", ephemeral=True)
        await handle_force_close(interaction)

async def handle_force_close(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    ticket = await tickets_collection.find_one({"discord_msg_id": interaction.message.id})
    if ticket:
        await GitHubAPI.close_issue(ticket['github_issue_num'])
        if ticket['thread_id']:
            try:
                thread = interaction.guild.get_thread(ticket['thread_id'])
                if thread: await thread.edit(archived=True, locked=True)
            except Exception: pass
        await send_audit_log(interaction.guild, interaction.user, "إغلاق إجباري 🗑️", ticket['github_issue_num'], ticket['title'], 0x992D22)
        await tickets_collection.delete_one({"discord_msg_id": interaction.message.id})
        await interaction.message.delete()
        await interaction.followup.send("🗑️ تم الحذف.", ephemeral=True)

# --------------------------------------------------------
# 🚀 الإقـلاع والأوامر المخصصة
# --------------------------------------------------------
@tasks.loop(hours=168)
async def archive_old_tickets_task():
    done_channel = bot.get_channel(DONE_CHANNEL_ID)
    archive_channel = bot.get_channel(ARCHIVE_CHANNEL_ID)
    if not done_channel or not archive_channel: return
    async for message in done_channel.history(limit=200):
        now = datetime.datetime.now(datetime.timezone.utc)
        if (now - message.created_at).days >= 7:
            if message.embeds:
                await archive_channel.send(embed=message.embeds[0])
                await tickets_collection.delete_one({"discord_msg_id": message.id})
                await message.delete()

@bot.event
async def on_ready():
    bot.add_view(DashboardView()) 
    bot.add_view(InitialTicketBootstrapView())
    bot.add_view(BugBootstrapView())
    bot.add_view(PersistentTicketOpsView())
    bot.add_view(DoneTicketOpsView())
    bot.add_view(SuspendedTicketOpsView())
    
    if not archive_old_tickets_task.is_running(): archive_old_tickets_task.start()
    bot.loop.create_task(fake_web_server())
    print(f"🔥 النظام الإداري المتقدم أونلاين! البوت: {bot.user}")

@bot.command()
@commands.has_permissions(administrator=True)
async def setup_tickets(ctx):
    embed = discord.Embed(title=f"{RTL} 💼 بوابة إدارة المهام", description=f"{RTL}اضغط لفتح تذكرة جديدة.", color=0x2C3E50)
    await ctx.send(embed=embed, view=InitialTicketBootstrapView())
    await ctx.message.delete()

@bot.command()
@commands.has_permissions(administrator=True)
async def setup_bugs(ctx):
    embed = discord.Embed(title=f"{RTL} 🐞 بوابة الإبلاغ عن الأخطاء", description=f"{RTL}اضغط لفتح تذكرة للإبلاغ عن مشكلة.", color=0xC0392B)
    await ctx.send(embed=embed, view=BugBootstrapView())
    await ctx.message.delete()

bot.run(TOKEN)