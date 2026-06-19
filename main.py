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

# حيل العرض والتنسيق
RTL = "\u202b"
WIDTH_HACK = "\u2800" * 45  # مسافات مخفية لإجبار البطاقة على التمدد أفقياً

# --------------------------------------------------------
# 🌐 خادم الويب الوهمي لـ Render
# --------------------------------------------------------
async def fake_web_server():
    app = web.Application()
    app.router.add_get('/', lambda request: web.Response(text="Gestax Discord Bot is Alive! 🚀 (MongoDB Active)"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 [HACK] Fake Web Server is listening on port {port} to trick Render!")

# --------------------------------------------------------
# 🐙 GitHub API
# --------------------------------------------------------
class GitHubAPI:
    @staticmethod
    async def create_issue(title: str, body: str, label: str) -> int:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/issues"
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        payload = {"title": title, "body": body, "labels": [label]}
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

# --------------------------------------------------------
# 📝 الاستمارات (حل مشكلة محاذاة العناوين جهة اليمين)
# --------------------------------------------------------
class TicketCreationModal(discord.ui.Modal):
    def __init__(self, selected_type: str, is_custom: bool):
        super().__init__(title="تفاصيل التذكرة البرمجية")
        self.selected_type = selected_type
        self.is_custom = is_custom

        self.ticket_title = discord.ui.TextInput(label="عنوان المهمة", placeholder="مثال: إضافة زر الدفع...")
        self.add_item(self.ticket_title)

        if self.is_custom:
            self.custom_type = discord.ui.TextInput(label="تصنيف المهمة (كتابة حرة)", placeholder="اكتب نوع القسم هنا...")
            self.add_item(self.custom_type)

        self.ticket_desc = discord.ui.TextInput(label="تفاصيل المهمة المطلوبة", style=discord.TextStyle.long)
        self.add_item(self.ticket_desc)

        self.ticket_dod = discord.ui.TextInput(label="Definition of Done (متى نعتبرها منتهية؟)", style=discord.TextStyle.long, placeholder="مثال: عندما تنجح عملية الدفع بدون أخطاء.")
        self.add_item(self.ticket_dod)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        final_type = self.custom_type.value.strip() if self.is_custom else self.selected_type

        issue_body = f"Requested by: {interaction.user.name}\n\n**Description:**\n{self.ticket_desc.value}\n\n**Definition of Done:**\n{self.ticket_dod.value}"
        issue_num = await GitHubAPI.create_issue(self.ticket_title.value, issue_body, final_type)
        
        if not issue_num:
            await interaction.followup.send("❌ فشل فتح التذكرة في جيتهاب.", ephemeral=True)
            return
        
        # تم استبدال العناوين الماركدوان بـ نصوص عريضة RTL لتجبر ديسكورد على المحاذاة لليمين
        embed = discord.Embed(
            title=f"{RTL} ⏳ تذكرة #{issue_num} | {self.ticket_title.value}",
            description=f"{RTL}**القسم:** {final_type}\n\n"
                        f"**{RTL}📋 الوصف:**\n{RTL}{self.ticket_desc.value}\n\n"
                        f"**{RTL}🎯 متطلبات الإنهاء (DoD):**\n{RTL}{self.ticket_dod.value}",
            color=0xF1C40F
        )
        avatar_url = interaction.user.avatar.url if interaction.user.avatar else interaction.user.default_avatar.url
        embed.set_author(name=f"بواسطة: {interaction.user.display_name}", icon_url=avatar_url)
        embed.set_footer(text=f"Gestax HQ • Pending{WIDTH_HACK}", icon_url="https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png")
        
        pending_channel = bot.get_channel(PENDING_CHANNEL_ID)
        msg = await pending_channel.send(embed=embed, view=PersistentTicketOpsView())
        
        # حفظ في MongoDB
        ticket_data = {
            "discord_msg_id": msg.id,
            "github_issue_num": issue_num,
            "creator_id": interaction.user.id,
            "assignee_id": None,
            "thread_id": None,
            "status": "PENDING",
            "ticket_type": final_type,
            "title": self.ticket_title.value,
            "description": self.ticket_desc.value,
            "dod": self.ticket_dod.value,
            "created_at": datetime.datetime.now(datetime.timezone.utc)
        }
        await tickets_collection.insert_one(ticket_data)

        await interaction.followup.send("✅ تم إرسال التذكرة بنجاح.", ephemeral=True)

class TicketTypeSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="برمجية", description="أكواد، خوارزميات، أو ميزات", emoji="💻"),
            discord.SelectOption(label="تصميم", description="تعديل في الـ UI/UX", emoji="🎨"),
            discord.SelectOption(label="إدارية", description="مهام تنظيمية", emoji="📁"),
            discord.SelectOption(label="أخرى (كتابة حرة)", description="للإدارة فقط", emoji="⚙️", value="custom")
        ]
        super().__init__(placeholder="اختر قسم التذكرة...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        val = self.values[0]
        is_custom = (val == "custom")
        if is_custom and not any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles):
            await interaction.response.send_message("❌ عذراً، هذا الخيار للإدارة العليا فقط.", ephemeral=True)
            return
        await interaction.response.send_modal(TicketCreationModal(selected_type=val, is_custom=is_custom))

class TicketTypeSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(TicketTypeSelect())

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
            description=f"{RTL}**القسم:** {ticket['ticket_type']}\n\n"
                        f"**{RTL}📋 الوصف:**\n{RTL}{ticket['description']}\n\n"
                        f"**{RTL}🎯 متطلبات الإنهاء (DoD):**\n{RTL}{ticket['dod']}\n\n"
                        f"**{RTL}⚠️ سبب الإيقاف:**\n{RTL}{self.reason.value}", 
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
        
        # التحديث في MongoDB
        await tickets_collection.update_one(
            {"discord_msg_id": self.msg_id},
            {"$set": {"discord_msg_id": new_msg.id, "status": "SUSPENDED"}}
        )
        
        if ticket['thread_id']:
            try:
                thread = interaction.guild.get_thread(ticket['thread_id'])
                if thread: await thread.send("🛑 **تنبيه:** تم تعليق العمل على هذه المهمة ونقلها للانتظار.")
            except: pass

        await interaction.message.delete()
        await interaction.followup.send("⏸️ تم نقل التذكرة لقناة (التذاكر المعلقة).", ephemeral=True)

# --------------------------------------------------------
# 👥 التعيين وغرف العمل الخاصة
# --------------------------------------------------------
class AssigneeSelect(discord.ui.UserSelect):
    def __init__(self, msg_id: int):
        super().__init__(custom_id=f"assign_select_menu:{msg_id}", placeholder="اختر العضو لتسليمه هذه المهمة...", min_values=1, max_values=1)
        self.msg_id = msg_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        assigned_developer = self.values[0]

        assigned_member = interaction.guild.get_member(assigned_developer.id)
        if assigned_member and any(role.id == BOT_ROLE_ID for role in assigned_member.roles):
            await interaction.followup.send("❌ لا يمكنك تعيين روبوت/بوت لهذه المهمة! اختر مطوراً بشرياً.", ephemeral=True)
            return

        ticket = await tickets_collection.find_one({"discord_msg_id": self.msg_id})
        if not ticket: return

        work_embed = discord.Embed(
            title=f"{RTL} 👨‍💻 قيد العمل #{ticket['github_issue_num']} | {ticket['title']}", 
            description=f"{RTL}**القسم:** {ticket['ticket_type']}\n\n"
                        f"**{RTL}📋 الوصف:**\n{RTL}{ticket['description']}\n\n"
                        f"**{RTL}🎯 متطلبات الإنهاء (DoD):**\n{RTL}{ticket['dod']}", 
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
            await tickets_collection.update_one(
                {"discord_msg_id": self.msg_id},
                {"$set": {"assignee_id": assigned_developer.id, "status": "IN_PROGRESS"}}
            )
            await interaction.followup.send("🔄 تم تحديث المطور.", ephemeral=True)
        else:
            in_progress_channel = bot.get_channel(IN_PROGRESS_CHANNEL_ID)
            new_msg = await in_progress_channel.send(embed=work_embed, view=PersistentTicketOpsView())
            thread = await in_progress_channel.create_thread(name=f"🔒-عمل-تذكرة-{ticket['github_issue_num']}", type=discord.ChannelType.private_thread, auto_archive_duration=1440)
            
            await thread.add_user(interaction.user)
            if ticket['creator_id']: await thread.add_user(interaction.guild.get_member(ticket['creator_id']))
            await thread.add_user(assigned_developer)
            await thread.send(f"⚠️ **مساحة عمل سرية:** تم عزل هذه الغرفة لمناقشة التذكرة رقم #{ticket['github_issue_num']}.")

            await tickets_collection.update_one(
                {"discord_msg_id": self.msg_id},
                {"$set": {"discord_msg_id": new_msg.id, "assignee_id": assigned_developer.id, "thread_id": thread.id, "status": "IN_PROGRESS"}}
            )
            try:
                await interaction.message.delete()
            except Exception: pass
            await interaction.followup.send("🎯 تم نقل التذكرة لقناة (قيد العمل) وتأسيس الغرفة.", ephemeral=True)

class AssigneeSelectView(discord.ui.View):
    def __init__(self, msg_id: int):
        super().__init__(timeout=60)
        self.add_item(AssigneeSelect(msg_id))

# --------------------------------------------------------
# 🔘 أزرار التذاكر المعلقة
# --------------------------------------------------------
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
            description=f"{RTL}**القسم:** {ticket['ticket_type']}\n\n"
                        f"**{RTL}📋 الوصف:**\n{RTL}{ticket['description']}\n\n"
                        f"**{RTL}🎯 متطلبات الإنهاء (DoD):**\n{RTL}{ticket['dod']}",
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
                if thread: await thread.send("▶️ **تنبيه:** تم نزع التعليق عن المهمة، يمكنكم مواصلة العمل الآن.")
            except: pass

        await tickets_collection.update_one(
            {"discord_msg_id": interaction.message.id},
            {"$set": {"discord_msg_id": new_msg.id, "status": "PENDING"}}
        )
        await interaction.message.delete()
        await interaction.followup.send("🔙 تم إعادة التذكرة لقناة (قيد الانتظار).", ephemeral=True)

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
            await interaction.followup.send("🗑️ تم حذف التذكرة.", ephemeral=True)

# --------------------------------------------------------
# 🔘 أزرار التذاكر المنتهية والأرشيف
# --------------------------------------------------------
class DoneTicketOpsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="أرشفة التذكرة 📁", style=discord.ButtonStyle.secondary, custom_id="btn_archive_done")
    async def archive_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles):
            await interaction.response.send_message("❌ الإدارة فقط يمكنها أرشفة المهام.", ephemeral=True)
            return
        archive_channel = bot.get_channel(ARCHIVE_CHANNEL_ID)
        await archive_channel.send(embed=interaction.message.embeds[0])
        await tickets_collection.delete_one({"discord_msg_id": interaction.message.id})
        await interaction.message.delete()
        await interaction.response.send_message("📁 تم نقل التذكرة للأرشيف.", ephemeral=True)

# --------------------------------------------------------
# 🔘 الأزرار الثابتة الرئيسية (للتذاكر النشطة)
# --------------------------------------------------------
class PersistentTicketOpsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="تعيين 👤", style=discord.ButtonStyle.primary, custom_id="btn_global_assign")
    async def assign_dev(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("اختر العضو من القائمة:", view=AssigneeSelectView(interaction.message.id), ephemeral=True)

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
            await interaction.followup.send("❌ فقط المطور المسؤول أو الإدارة يحق لهم إنهاء هذه المهمة.", ephemeral=True)
            return

        await GitHubAPI.close_issue(ticket['github_issue_num'])
        done_channel = bot.get_channel(DONE_CHANNEL_ID)
        
        done_embed = discord.Embed(
            title=f"{RTL} 🎉 مكتملة #{ticket['github_issue_num']} | {ticket['title']}", 
            description=f"{RTL}**القسم:** {ticket['ticket_type']}\n\n"
                        f"**{RTL}📋 الوصف:**\n{RTL}{ticket['description']}\n\n"
                        f"**{RTL}🎯 متطلبات الإنهاء (DoD):**\n{RTL}{ticket['dod']}", 
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

        await tickets_collection.update_one(
            {"discord_msg_id": interaction.message.id},
            {"$set": {"discord_msg_id": new_msg.id, "status": "DONE"}}
        )
        await interaction.message.delete()
        await interaction.followup.send("✅ تم اعتماد التذكرة ونقلها لقناة المكتملة.", ephemeral=True)

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
            await interaction.followup.send("🗑️ تم حذف التذكرة.", ephemeral=True)

class InitialTicketBootstrapView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="فتح تذكرة مهام 📝", style=discord.ButtonStyle.success, custom_id="btn_trigger_select_menu")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("اختر نوع التذكرة البرمجية التي تريد فتحها:", view=TicketTypeSelectView(), ephemeral=True)

# --------------------------------------------------------
# ⏰ الأرشفة الدورية (أسبوعياً)
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

# --------------------------------------------------------
# 🚀 الإقـلاع
# --------------------------------------------------------
@bot.event
async def on_ready():
    bot.add_view(InitialTicketBootstrapView())
    bot.add_view(PersistentTicketOpsView())
    bot.add_view(DoneTicketOpsView())
    bot.add_view(SuspendedTicketOpsView())
    
    if not archive_old_tickets_task.is_running(): archive_old_tickets_task.start()
    bot.loop.create_task(fake_web_server())
    print(f"🔥 النظام المدمّر أونلاين، تم ربط قاعدة بيانات MongoDB بنجاح! البوت: {bot.user}")

@bot.command()
@commands.has_permissions(administrator=True)
async def setup_tickets(ctx):
    embed = discord.Embed(
        title=f"{RTL} 💼 بوابة إدارة المهام", 
        description=f"{RTL}اضغط لفتح تذكرة جديدة. سيُطلب منك تحديد نوع المهمة (برمجية، تصميم، إلخ).", 
        color=0x2C3E50
    )
    await ctx.send(embed=embed, view=InitialTicketBootstrapView())
    await ctx.message.delete()

bot.run(TOKEN)