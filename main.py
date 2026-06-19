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

# --------------------------------------------------------
# 🗄️ إعداد MongoDB
# --------------------------------------------------------
mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
db = mongo_client["gestax_system"]
tickets_collection = db["tickets"]

RTL = "\u202b"
WIDTH_HACK = "\u2800" * 45  

# --------------------------------------------------------
# 🌐 خادم الويب الوهمي لـ Render
# --------------------------------------------------------
async def fake_web_server():
    app = web.Application()
    app.router.add_get('/', lambda request: web.Response(text="Gestax Discord Bot is Alive! 🚀"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

# --------------------------------------------------------
# 🐙 GitHub API (محدث لدعم قوائم الـ Labels المتعددة)
# --------------------------------------------------------
class GitHubAPI:
    @staticmethod
    async def create_issue(title: str, body: str, labels: list) -> int:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/issues"
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        payload = {"title": title, "body": body, "labels": labels}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status == 201:
                    return (await resp.json())["number"]
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
# 1️⃣ القسم الأول: نظام المهام المعتاد (Standard Tickets)
# ========================================================
class TicketCreationModal(discord.ui.Modal):
    def __init__(self, category: str, kind: str, urgency: str, is_custom: bool):
        super().__init__(title="تفاصيل التذكرة البرمجية")
        self.category = category
        self.kind = kind
        self.urgency = urgency
        self.is_custom = is_custom

        self.ticket_title = discord.ui.TextInput(label="عنوان المهمة", placeholder="مثال: إضافة نظام الدفع...")
        self.add_item(self.ticket_title)

        if self.is_custom:
            self.custom_type = discord.ui.TextInput(label="تصنيف القسم (كتابة حرة)", placeholder="اكتب نوع القسم هنا...")
            self.add_item(self.custom_type)

        self.ticket_desc = discord.ui.TextInput(label="تفاصيل المهمة المطلوبة", style=discord.TextStyle.long)
        self.add_item(self.ticket_desc)

        self.ticket_dod = discord.ui.TextInput(label="Definition of Done (متى نعتبرها منتهية؟)", style=discord.TextStyle.long)
        self.add_item(self.ticket_dod)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        final_category = self.custom_type.value.strip() if self.is_custom else self.category

        gh_labels = [final_category, self.kind, self.urgency]
        issue_body = f"Requested by: {interaction.user.name}\n\n**Description:**\n{self.ticket_desc.value}\n\n**DoD:**\n{self.ticket_dod.value}"
        
        issue_num = await GitHubAPI.create_issue(self.ticket_title.value, issue_body, gh_labels)
        if not issue_num:
            await interaction.followup.send("❌ فشل فتح التذكرة في جيتهاب.", ephemeral=True)
            return
        
        formatted_body = f"**{RTL}📋 الوصف:**\n{RTL}{self.ticket_desc.value}\n\n**{RTL}🎯 متطلبات الإنهاء (DoD):**\n{RTL}{self.ticket_dod.value}"
        
        embed = discord.Embed(
            title=f"{RTL} ⏳ تذكرة #{issue_num} | {self.ticket_title.value}",
            description=f"{RTL}**القسم:** {final_category} | **النوع:** {self.kind} | **الأولوية:** {self.urgency}\n\n{formatted_body}",
            color=0xF1C40F
        )
        avatar_url = interaction.user.avatar.url if interaction.user.avatar else interaction.user.default_avatar.url
        embed.set_author(name=f"بواسطة: {interaction.user.display_name}", icon_url=avatar_url)
        embed.set_footer(text=f"Gestax HQ • Pending{WIDTH_HACK}", icon_url="https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png")
        
        pending_channel = bot.get_channel(PENDING_CHANNEL_ID)
        msg = await pending_channel.send(embed=embed, view=PersistentTicketOpsView())
        
        ticket_data = {
            "discord_msg_id": msg.id,
            "github_issue_num": issue_num,
            "creator_id": interaction.user.id,
            "assignee_id": None,
            "thread_id": None,
            "status": "PENDING",
            "category": final_category,
            "kind": self.kind,
            "urgency": self.urgency,
            "title": self.ticket_title.value,
            "formatted_body": formatted_body,
            "created_at": datetime.datetime.now(datetime.timezone.utc)
        }
        await tickets_collection.insert_one(ticket_data)
        await interaction.followup.send("✅ تم إرسال التذكرة بنجاح.", ephemeral=True)

class TicketConfigView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.category = None
        self.kind = None
        self.urgency = None

    @discord.ui.select(placeholder="1️⃣ القسم (اختر القسم الخاص بالمهمة)...", options=[
        discord.SelectOption(label="برمجية", emoji="💻"),
        discord.SelectOption(label="تصميم", emoji="🎨"),
        discord.SelectOption(label="إدارية", emoji="📁"),
        discord.SelectOption(label="أخرى (كتابة حرة)", emoji="⚙️", value="custom")
    ])
    async def select_category(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.category = select.values[0]
        await interaction.response.defer()

    @discord.ui.select(placeholder="2️⃣ نوع المهمة (تاسك أم فيتشور)...", options=[
        discord.SelectOption(label="ميزة جديدة (Feature)", emoji="✨"),
        discord.SelectOption(label="مهمة عمل (Task)", emoji="📋")
    ])
    async def select_kind(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.kind = select.values[0]
        await interaction.response.defer()

    @discord.ui.select(placeholder="3️⃣ مدى الاستعجال (الأولوية)...", options=[
        discord.SelectOption(label="منخفضة (Low)", emoji="🟢"),
        discord.SelectOption(label="متوسطة (Medium)", emoji="🟡"),
        discord.SelectOption(label="عالية (High)", emoji="🟠"),
        discord.SelectOption(label="حرجة (Critical)", emoji="🔴")
    ])
    async def select_urgency(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.urgency = select.values[0]
        await interaction.response.defer()

    @discord.ui.button(label="متابعة وكتابة التفاصيل ➡️", style=discord.ButtonStyle.success, row=3)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.category or not self.kind or not self.urgency:
            await interaction.response.send_message("❌ يرجى اختيار القسم، النوع، ومدى الاستعجال أولاً من القوائم أعلاه!", ephemeral=True)
            return
        is_custom = (self.category == "custom")
        if is_custom and not any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles):
            await interaction.response.send_message("❌ عذراً، خيار (أخرى) للإدارة العليا فقط.", ephemeral=True)
            return
        await interaction.response.send_modal(TicketCreationModal(self.category, self.kind, self.urgency, is_custom))

class InitialTicketBootstrapView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="فتح تذكرة مهام 📝", style=discord.ButtonStyle.primary, custom_id="btn_open_standard")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("⚙️ **إعداد التذكرة:** يرجى تحديد الخيارات التالية ثم اضغط متابعة:", view=TicketConfigView(), ephemeral=True)

# ========================================================
# 2️⃣ القسم الثاني: نظام الأخطاء البرمجية (Bug Flow)
# ========================================================
class BugCreationModal(discord.ui.Modal):
    def __init__(self, urgency: str):
        super().__init__(title="الإبلاغ عن خلل برمجي (Bug)")
        self.urgency = urgency
        
        self.bug_title = discord.ui.TextInput(label="عنوان المشكلة باختصار", placeholder="مثال: تعطل التطبيق عند الضغط على زر الشراء...")
        self.add_item(self.bug_title)

        self.bug_reproduce = discord.ui.TextInput(label="خطوات إعادة الإنتاج (Steps to Reproduce)", style=discord.TextStyle.long, placeholder="1. افتح التطبيق\n2. اذهب إلى المتجر\n3. اضغط شراء...")
        self.add_item(self.bug_reproduce)

        self.bug_expected = discord.ui.TextInput(label="السلوك المتوقع (Expected Behavior)", style=discord.TextStyle.paragraph, placeholder="أن تظهر نافذة الدفع بنجاح.")
        self.add_item(self.bug_expected)

        self.bug_actual = discord.ui.TextInput(label="السلوك الفعلي/الخطأ (Actual Behavior)", style=discord.TextStyle.paragraph, placeholder="انهيار التطبيق وظهور شاشة بيضاء.")
        self.add_item(self.bug_actual)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        gh_labels = ["Bug 🐞", self.urgency]
        issue_body = f"Reported by: {interaction.user.name}\n\n**Steps to Reproduce:**\n{self.bug_reproduce.value}\n\n**Expected:**\n{self.bug_expected.value}\n\n**Actual:**\n{self.bug_actual.value}"
        
        issue_num = await GitHubAPI.create_issue(self.bug_title.value, issue_body, gh_labels)
        if not issue_num:
            await interaction.followup.send("❌ فشل فتح التذكرة في جيتهاب.", ephemeral=True)
            return
        
        formatted_body = f"**{RTL}🔄 خطوات إعادة الإنتاج:**\n{RTL}{self.bug_reproduce.value}\n\n**{RTL}🎯 السلوك المتوقع:**\n{RTL}{self.bug_expected.value}\n\n**{RTL}⚠️ السلوك الفعلي:**\n{RTL}{self.bug_actual.value}"
        
        embed = discord.Embed(
            title=f"{RTL} 🐞 بلاغ خطأ #{issue_num} | {self.bug_title.value}",
            description=f"{RTL}**القسم:** باق (Bug) 🐞 | **الأولوية:** {self.urgency}\n\n{formatted_body}",
            color=0xE74C3C
        )
        avatar_url = interaction.user.avatar.url if interaction.user.avatar else interaction.user.default_avatar.url
        embed.set_author(name=f"بواسطة: {interaction.user.display_name}", icon_url=avatar_url)
        embed.set_footer(text=f"Gestax HQ • Bug Report{WIDTH_HACK}", icon_url="https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png")
        
        pending_channel = bot.get_channel(PENDING_CHANNEL_ID)
        msg = await pending_channel.send(embed=embed, view=PersistentTicketOpsView())
        
        ticket_data = {
            "discord_msg_id": msg.id,
            "github_issue_num": issue_num,
            "creator_id": interaction.user.id,
            "assignee_id": None,
            "thread_id": None,
            "status": "PENDING",
            "category": "باق (Bug) 🐞",
            "kind": "Bug",
            "urgency": self.urgency,
            "title": self.bug_title.value,
            "formatted_body": formatted_body,
            "created_at": datetime.datetime.now(datetime.timezone.utc)
        }
        await tickets_collection.insert_one(ticket_data)
        await interaction.followup.send("✅ تم إرسال بلاغ الخطأ بنجاح.", ephemeral=True)

class BugConfigView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.urgency = None

    @discord.ui.select(placeholder="🚨 اختر مدى خطورة هذا العطل...", options=[
        discord.SelectOption(label="منخفضة (Low)", description="خطأ بصري أو غير مؤثر", emoji="🟢"),
        discord.SelectOption(label="متوسطة (Medium)", description="يؤثر بشكل طفيف", emoji="🟡"),
        discord.SelectOption(label="عالية (High)", description="يعيق ميزة أساسية", emoji="🟠"),
        discord.SelectOption(label="حرجة (Critical)", description="انهيار كامل (Crash)", emoji="🔴")
    ])
    async def select_urgency(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.urgency = select.values[0]
        await interaction.response.defer()

    @discord.ui.button(label="متابعة لكتابة تفاصيل الخطأ ➡️", style=discord.ButtonStyle.danger, row=1)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.urgency:
            await interaction.response.send_message("❌ يرجى تحديد خطورة العطل أولاً!", ephemeral=True)
            return
        await interaction.response.send_modal(BugCreationModal(self.urgency))

class BugBootstrapView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="إبلاغ عن عيب برمجي (Bug) 🐞", style=discord.ButtonStyle.danger, custom_id="btn_open_bug")
    async def open_bug(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("⚙️ **إعداد البلاغ:** حدد مستوى الخطورة:", view=BugConfigView(), ephemeral=True)

# ========================================================
# 3️⃣ العمليات المشتركة (تعليق، تعيين، إنهاء)
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
        avatar_url = creator_user.avatar.url if creator_user and creator_user.avatar else interaction.user.default_avatar.url
        embed.set_author(name=f"المنشئ: {creator_user.display_name if creator_user else 'غير معروف'}", icon_url=avatar_url)
        
        if ticket['assignee_id']:
            assignee_user = interaction.guild.get_member(ticket['assignee_id'])
            if assignee_user:
                embed.add_field(name=f"{RTL}المطور المسؤول", value=assignee_user.mention, inline=False)

        embed.set_footer(text=f"Gestax HQ • Suspended{WIDTH_HACK}")
        suspended_channel = bot.get_channel(SUSPENDED_CHANNEL_ID)
        new_msg = await suspended_channel.send(embed=embed, view=SuspendedTicketOpsView())
        
        await tickets_collection.update_one({"discord_msg_id": self.msg_id}, {"$set": {"discord_msg_id": new_msg.id, "status": "SUSPENDED"}})
        
        if ticket['thread_id']:
            try:
                thread = interaction.guild.get_thread(ticket['thread_id'])
                if thread: await thread.send("🛑 **تنبيه:** تم تعليق العمل على هذه المهمة ونقلها للانتظار.")
            except: pass

        await interaction.message.delete()
        await interaction.followup.send("⏸️ تم النقل لقناة التذاكر المعلقة.", ephemeral=True)

class AssigneeSelect(discord.ui.UserSelect):
    def __init__(self, msg_id: int):
        super().__init__(custom_id=f"assign_select_menu:{msg_id}", placeholder="اختر العضو لتسليمه هذه المهمة...", min_values=1, max_values=1)
        self.msg_id = msg_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        assigned_developer = self.values[0]

        assigned_member = interaction.guild.get_member(assigned_developer.id)
        if assigned_member and any(role.id == BOT_ROLE_ID for role in assigned_member.roles):
            await interaction.followup.send("❌ لا يمكنك تعيين روبوت!", ephemeral=True)
            return

        ticket = await tickets_collection.find_one({"discord_msg_id": self.msg_id})
        if not ticket: return

        work_embed = discord.Embed(
            title=f"{RTL} 👨‍💻 قيد العمل #{ticket['github_issue_num']} | {ticket['title']}", 
            description=f"{RTL}**القسم:** {ticket['category']} | **النوع:** {ticket['kind']} | **الأولوية:** {ticket['urgency']}\n\n{ticket['formatted_body']}", 
            color=0x3498DB
        )
        
        creator_user = interaction.guild.get_member(ticket['creator_id'])
        avatar_url = creator_user.avatar.url if creator_user and creator_user.avatar else interaction.user.default_avatar.url
        work_embed.set_author(name=f"المنشئ: {creator_user.display_name if creator_user else 'غير معروف'}", icon_url=avatar_url)
        work_embed.add_field(name=f"{RTL}المستلم", value=assigned_developer.mention, inline=False)
        work_embed.set_footer(text=f"Gestax HQ • In Progress{WIDTH_HACK}")
        
        if ticket['thread_id'] and ticket['status'] in ['IN_PROGRESS', 'SUSPENDED']:
            thread = interaction.guild.get_thread(ticket['thread_id'])
            if thread:
                await thread.add_user(assigned_developer)
                await thread.send(f"🔄 **تحديث المهمة:** تم تسليم هذه التذكرة للمطور {assigned_developer.mention}.")
            await interaction.message.edit(embed=work_embed)
            await tickets_collection.update_one({"discord_msg_id": self.msg_id}, {"$set": {"assignee_id": assigned_developer.id, "status": "IN_PROGRESS"}})
            await interaction.followup.send("🔄 تم تحديث المطور.", ephemeral=True)
        else:
            in_progress_channel = bot.get_channel(IN_PROGRESS_CHANNEL_ID)
            new_msg = await in_progress_channel.send(embed=work_embed, view=PersistentTicketOpsView())
            thread = await in_progress_channel.create_thread(name=f"🔒-عمل-{ticket['github_issue_num']}", type=discord.ChannelType.private_thread, auto_archive_duration=1440)
            
            await thread.add_user(interaction.user)
            if ticket['creator_id']: await thread.add_user(interaction.guild.get_member(ticket['creator_id']))
            await thread.add_user(assigned_developer)
            await thread.send(f"⚠️ **مساحة عمل سرية:** تم عزل هذه الغرفة لمناقشة رقم #{ticket['github_issue_num']}.")

            await tickets_collection.update_one({"discord_msg_id": self.msg_id}, {"$set": {"discord_msg_id": new_msg.id, "assignee_id": assigned_developer.id, "thread_id": thread.id, "status": "IN_PROGRESS"}})
            try:
                await interaction.message.delete()
            except Exception: pass
            await interaction.followup.send("🎯 تم نقل التذكرة وتأسيس الغرفة.", ephemeral=True)

class AssigneeSelectView(discord.ui.View):
    def __init__(self, msg_id: int):
        super().__init__(timeout=60)
        self.add_item(AssigneeSelect(msg_id))

class SuspendedTicketOpsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="نزع التعليق 🔙", style=discord.ButtonStyle.primary, custom_id="btn_unsuspend")
    async def unsuspend_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ticket = await tickets_collection.find_one({"discord_msg_id": interaction.message.id})
        if not ticket: return

        embed = discord.Embed(
            title=f"{RTL} ⏳ تذكرة #{ticket['github_issue_num']} | {ticket['title']}",
            description=f"{RTL}**القسم:** {ticket['category']} | **النوع:** {ticket['kind']} | **الأولوية:** {ticket['urgency']}\n\n{ticket['formatted_body']}",
            color=0xF1C40F
        )
        creator_user = interaction.guild.get_member(ticket['creator_id'])
        avatar_url = creator_user.avatar.url if creator_user and creator_user.avatar else interaction.user.default_avatar.url
        embed.set_author(name=f"المنشئ: {creator_user.display_name if creator_user else 'غير معروف'}", icon_url=avatar_url)
        embed.set_footer(text=f"Gestax HQ • Pending{WIDTH_HACK}")
        
        pending_channel = bot.get_channel(PENDING_CHANNEL_ID)
        new_msg = await pending_channel.send(embed=embed, view=PersistentTicketOpsView())

        if ticket['thread_id']:
            try:
                thread = interaction.guild.get_thread(ticket['thread_id'])
                if thread: await thread.send("▶️ **تنبيه:** تم نزع التعليق، يمكنكم مواصلة العمل الآن.")
            except: pass

        await tickets_collection.update_one({"discord_msg_id": interaction.message.id}, {"$set": {"discord_msg_id": new_msg.id, "status": "PENDING"}})
        await interaction.message.delete()
        await interaction.followup.send("🔙 عادت لقناة الانتظار.", ephemeral=True)

    @discord.ui.button(label="إغلاق إجباري ❌", style=discord.ButtonStyle.danger, custom_id="btn_suspend_force_close")
    async def force_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles):
            await interaction.response.send_message("❌ للإدارة فقط.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        ticket = await tickets_collection.find_one({"discord_msg_id": interaction.message.id})
        if ticket:
            await GitHubAPI.close_issue(ticket['github_issue_num'])
            if ticket['thread_id']:
                try:
                    thread = interaction.guild.get_thread(ticket['thread_id'])
                    if thread: await thread.edit(archived=True, locked=True)
                except Exception: pass
            await tickets_collection.delete_one({"discord_msg_id": interaction.message.id})
            await interaction.message.delete()
            await interaction.followup.send("🗑️ تم الحذف.", ephemeral=True)

class DoneTicketOpsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="أرشفة التذكرة 📁", style=discord.ButtonStyle.secondary, custom_id="btn_archive_done")
    async def archive_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles):
            await interaction.response.send_message("❌ الإدارة فقط.", ephemeral=True)
            return
        archive_channel = bot.get_channel(ARCHIVE_CHANNEL_ID)
        await archive_channel.send(embed=interaction.message.embeds[0])
        await tickets_collection.delete_one({"discord_msg_id": interaction.message.id})
        await interaction.message.delete()
        await interaction.response.send_message("📁 تم النقل للأرشيف.", ephemeral=True)

class PersistentTicketOpsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="تعيين 👤", style=discord.ButtonStyle.primary, custom_id="btn_global_assign")
    async def assign_dev(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("اختر العضو:", view=AssigneeSelectView(interaction.message.id), ephemeral=True)

    @discord.ui.button(label="تعليق ⏸️", style=discord.ButtonStyle.secondary, custom_id="btn_global_suspend")
    async def suspend_task(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SuspendModal(interaction.message.id))

    @discord.ui.button(label="إنهاء ✅", style=discord.ButtonStyle.success, custom_id="btn_mark_done")
    async def mark_done(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ticket = await tickets_collection.find_one({"discord_msg_id": interaction.message.id})
        if not ticket: return

        is_admin = any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles)
        if not is_admin and interaction.user.id != ticket['assignee_id']:
            await interaction.followup.send("❌ فقط المطور المسؤول أو الإدارة يحق لهم الإنهاء.", ephemeral=True)
            return

        await GitHubAPI.close_issue(ticket['github_issue_num'])
        done_channel = bot.get_channel(DONE_CHANNEL_ID)
        
        done_embed = discord.Embed(
            title=f"{RTL} 🎉 مكتملة #{ticket['github_issue_num']} | {ticket['title']}", 
            description=f"{RTL}**القسم:** {ticket['category']} | **النوع:** {ticket['kind']} | **الأولوية:** {ticket['urgency']}\n\n{ticket['formatted_body']}", 
            color=0x2ECC71
        )
        done_embed.set_footer(text=f"Gestax HQ • Completed • {datetime.datetime.now().strftime('%Y-%m-%d')}{WIDTH_HACK}")
        
        new_msg = await done_channel.send(embed=done_embed, view=DoneTicketOpsView())

        if ticket['thread_id']:
            try:
                thread = interaction.guild.get_thread(ticket['thread_id'])
                if thread:
                    await thread.send("🔒 **مكتمل:** تم إنهاء المهمة وقفل الغرفة بنجاح.")
                    await thread.edit(archived=True, locked=True)
            except Exception: pass

        await tickets_collection.update_one({"discord_msg_id": interaction.message.id}, {"$set": {"discord_msg_id": new_msg.id, "status": "DONE"}})
        await interaction.message.delete()
        await interaction.followup.send("✅ تم الاعتماد والنقل للمكتملة.", ephemeral=True)

    @discord.ui.button(label="إغلاق ❌", style=discord.ButtonStyle.danger, custom_id="btn_global_force_close")
    async def force_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles):
            await interaction.response.send_message("❌ للإدارة فقط.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        ticket = await tickets_collection.find_one({"discord_msg_id": interaction.message.id})
        if ticket:
            await GitHubAPI.close_issue(ticket['github_issue_num'])
            if ticket['thread_id']:
                try:
                    thread = interaction.guild.get_thread(ticket['thread_id'])
                    if thread: await thread.edit(archived=True, locked=True)
                except Exception: pass
            await tickets_collection.delete_one({"discord_msg_id": interaction.message.id})
            await interaction.message.delete()
            await interaction.followup.send("🗑️ تم الحذف.", ephemeral=True)

# --------------------------------------------------------
# 🚀 الإقـلاع والأوامر المخصصة للقنوات
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
    bot.add_view(InitialTicketBootstrapView())
    bot.add_view(BugBootstrapView())
    bot.add_view(PersistentTicketOpsView())
    bot.add_view(DoneTicketOpsView())
    bot.add_view(SuspendedTicketOpsView())
    
    if not archive_old_tickets_task.is_running(): archive_old_tickets_task.start()
    bot.loop.create_task(fake_web_server())
    print(f"🔥 النظام أونلاين بوضعية مهام + باقات! البوت: {bot.user}")

# أمر قناة المهام العادية
@bot.command()
@commands.has_permissions(administrator=True)
async def setup_tickets(ctx):
    embed = discord.Embed(
        title=f"{RTL} 💼 بوابة إدارة المهام", 
        description=f"{RTL}اضغط لفتح تذكرة جديدة. سيُطلب منك تحديد القسم، النوع، ومدى الاستعجال.", 
        color=0x2C3E50
    )
    await ctx.send(embed=embed, view=InitialTicketBootstrapView())
    await ctx.message.delete()

# أمر قناة الأخطاء البرمجية (Bugs)
@bot.command()
@commands.has_permissions(administrator=True)
async def setup_bugs(ctx):
    embed = discord.Embed(
        title=f"{RTL} 🐞 بوابة الإبلاغ عن الأخطاء (Bug Tracker)", 
        description=f"{RTL}هل واجهتك مشكلة في النظام أو التطبيق؟\nاضغط هنا لفتح تذكرة للإبلاغ عن المشكلة ليتم إصلاحها بأسرع وقت.", 
        color=0xC0392B
    )
    await ctx.send(embed=embed, view=BugBootstrapView())
    await ctx.message.delete()

bot.run(TOKEN)