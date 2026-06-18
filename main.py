import os
import discord
from discord.ext import commands, tasks
import aiohttp
from aiohttp import web 
import aiosqlite
import datetime
import asyncio
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")
BOOTSTRAP_CHANNEL_ID = int(os.getenv("CHANNEL_BOOTSTRAP_ID"))
PENDING_CHANNEL_ID = int(os.getenv("CHANNEL_NEW_TICKETS_ID"))
IN_PROGRESS_CHANNEL_ID = int(os.getenv("CHANNEL_IN_PROGRESS_ID"))
SUSPENDED_CHANNEL_ID = int(os.getenv("CHANNEL_SUSPENDED_ID")) # القناة الجديدة
DONE_CHANNEL_ID = int(os.getenv("CHANNEL_DONE_ID"))
ARCHIVE_CHANNEL_ID = int(os.getenv("CHANNEL_ARCHIVE_ID"))
ADMIN_ROLE_ID = int(os.getenv("ROLE_ADMIN_ID"))
BOT_ROLE_ID = int(os.getenv("ROLE_BOT_ID"))

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
DB_FILE = "gestax_system.db"

# --------------------------------------------------------
# 🌐 خادم الويب الوهمي لـ Render
# --------------------------------------------------------
async def fake_web_server():
    app = web.Application()
    app.router.add_get('/', lambda request: web.Response(text="Gestax Discord Bot is Alive! 🚀 (Render Trick Active)"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 [HACK] Fake Web Server is listening on port {port} to trick Render!")

# --------------------------------------------------------
# 🗄️ قاعدة البيانات
# --------------------------------------------------------
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS tickets (
                discord_msg_id INTEGER PRIMARY KEY,
                github_issue_num INTEGER,
                creator_id INTEGER,
                assignee_id INTEGER,
                thread_id INTEGER,
                status TEXT,
                ticket_type TEXT,
                title TEXT,
                description TEXT,
                dod TEXT DEFAULT 'غير محدد'
            )
        ''')
        await db.commit()

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
# 📝 الاستمارات (مع تكبير الخط وإزالة الحالة)
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
        
        # استخدام ### لتكبير الخط بشكل ملحوظ داخل الـ Embed
        embed = discord.Embed(
            title=f"⏳ #{issue_num} | {final_type} | {self.ticket_title.value}",
            description=f"### الوصف:\n{self.ticket_desc.value}\n\n### 🎯 متطلبات الإنهاء (DoD):\n{self.ticket_dod.value}",
            color=discord.Color.gold()
        )
        embed.add_field(name="المنشئ", value=interaction.user.mention, inline=False)
        
        pending_channel = bot.get_channel(PENDING_CHANNEL_ID)
        msg = await pending_channel.send(embed=embed, view=PersistentTicketOpsView())
        
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT INTO tickets (discord_msg_id, github_issue_num, creator_id, assignee_id, thread_id, status, ticket_type, title, description, dod) VALUES (?, ?, ?, NULL, NULL, 'PENDING', ?, ?, ?, ?)",
                (msg.id, issue_num, interaction.user.id, final_type, self.ticket_title.value, self.ticket_desc.value, self.ticket_dod.value)
            )
            await db.commit()

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
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT github_issue_num, creator_id, ticket_type, title, description, assignee_id, dod, thread_id FROM tickets WHERE discord_msg_id = ?", (self.msg_id,)) as cursor:
                row = await cursor.fetchone()
                if not row: return
                issue_num, creator_id, t_type, title, desc, assignee_id, dod, thread_id = row

                embed = discord.Embed(
                    title=f"🛑 #{issue_num} | {t_type} | {title}", 
                    description=f"### الوصف:\n{desc}\n\n### 🎯 متطلبات الإنهاء (DoD):\n{dod}\n\n### ⚠️ سبب الإيقاف:\n{self.reason.value}", 
                    color=discord.Color.red()
                )
                creator_user = interaction.guild.get_member(creator_id)
                assignee_user = interaction.guild.get_member(assignee_id) if assignee_id else None
                
                embed.add_field(name="المنشئ", value=creator_user.mention if creator_user else "غير معروف", inline=True)
                embed.add_field(name="المطور المسؤول", value=assignee_user.mention if assignee_user else "غير محدد", inline=True)
                
                suspended_channel = bot.get_channel(SUSPENDED_CHANNEL_ID)
                # إرسال الرسالة إلى قناة المعلقة مع الأزرار الخاصة بها
                new_msg = await suspended_channel.send(embed=embed, view=SuspendedTicketOpsView())
                
                await db.execute("UPDATE tickets SET discord_msg_id = ?, status = 'SUSPENDED' WHERE discord_msg_id = ?", (new_msg.id, self.msg_id))
                await db.commit()
                
                if thread_id:
                    try:
                        thread = interaction.guild.get_thread(thread_id)
                        if thread: await thread.send("🛑 **تنبيه:** تم تعليق العمل على هذه المهمة ونقلها للانتظار.")
                    except: pass

                await interaction.message.delete()
                
        await interaction.followup.send("⏸️ تم نقل التذكرة لقناة (التذاكر المعلقة).", ephemeral=True)

# --------------------------------------------------------
# 👥 التعيين
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

        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT github_issue_num, creator_id, ticket_type, title, description, thread_id, status, dod FROM tickets WHERE discord_msg_id = ?", (self.msg_id,)) as cursor:
                row = await cursor.fetchone()
                if not row: return
                issue_num, creator_id, t_type, title, desc, thread_id, status, dod = row

            work_embed = discord.Embed(
                title=f"👨‍💻 #{issue_num} | {t_type} | {title}", 
                description=f"### الوصف:\n{desc}\n\n### 🎯 متطلبات الإنهاء (DoD):\n{dod}", 
                color=discord.Color.blue()
            )
            work_embed.add_field(name="المستلم", value=assigned_developer.mention, inline=False)
            
            if thread_id and status in ['IN_PROGRESS', 'SUSPENDED']:
                thread = interaction.guild.get_thread(thread_id)
                if thread:
                    await thread.add_user(assigned_developer)
                    await thread.send(f"🔄 **تحديث المهمة:** تم تسليم هذه التذكرة للمطور {assigned_developer.mention}.")
                await interaction.message.edit(embed=work_embed)
                await db.execute("UPDATE tickets SET assignee_id = ?, status = 'IN_PROGRESS' WHERE discord_msg_id = ?", (assigned_developer.id, self.msg_id))
                await interaction.followup.send("🔄 تم تحديث المطور.", ephemeral=True)
            else:
                in_progress_channel = bot.get_channel(IN_PROGRESS_CHANNEL_ID)
                new_msg = await in_progress_channel.send(embed=work_embed, view=PersistentTicketOpsView())
                thread = await in_progress_channel.create_thread(name=f"🔒-عمل-تذكرة-{issue_num}", type=discord.ChannelType.private_thread, auto_archive_duration=1440)
                
                await thread.add_user(interaction.user)
                if creator_id: await thread.add_user(interaction.guild.get_member(creator_id))
                await thread.add_user(assigned_developer)
                await thread.send(f"⚠️ **مساحة عمل سرية:** تم عزل هذه الغرفة لمناقشة التذكرة رقم #{issue_num}.")

                await db.execute("UPDATE tickets SET discord_msg_id = ?, assignee_id = ?, thread_id = ?, status = 'IN_PROGRESS' WHERE discord_msg_id = ?", (new_msg.id, assigned_developer.id, thread.id, self.msg_id))
                try:
                    await interaction.message.delete()
                except Exception: pass
                await interaction.followup.send("🎯 تم نقل التذكرة لقناة (قيد العمل) وتأسيس الغرفة.", ephemeral=True)
            await db.commit()

class AssigneeSelectView(discord.ui.View):
    def __init__(self, msg_id: int):
        super().__init__(timeout=60)
        self.add_item(AssigneeSelect(msg_id))

# --------------------------------------------------------
# 🔘 أزرار التذاكر المعلقة (جديد)
# --------------------------------------------------------
class SuspendedTicketOpsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="نزع التعليق (إعادة للانتظار) 🔙", style=discord.ButtonStyle.primary, custom_id="btn_unsuspend")
    async def unsuspend_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT github_issue_num, creator_id, ticket_type, title, description, dod, thread_id FROM tickets WHERE discord_msg_id = ?", (interaction.message.id,)) as cursor:
                row = await cursor.fetchone()
                if not row: return
                issue_num, creator_id, t_type, title, desc, dod, thread_id = row

            embed = discord.Embed(
                title=f"⏳ #{issue_num} | {t_type} | {title}",
                description=f"### الوصف:\n{desc}\n\n### 🎯 متطلبات الإنهاء (DoD):\n{dod}",
                color=discord.Color.gold()
            )
            creator_user = interaction.guild.get_member(creator_id)
            embed.add_field(name="المنشئ", value=creator_user.mention if creator_user else "غير معروف", inline=False)
            
            pending_channel = bot.get_channel(PENDING_CHANNEL_ID)
            new_msg = await pending_channel.send(embed=embed, view=PersistentTicketOpsView())

            if thread_id:
                try:
                    thread = interaction.guild.get_thread(thread_id)
                    if thread: await thread.send("▶️ **تنبيه:** تم نزع التعليق عن المهمة، يمكنكم مواصلة العمل الآن.")
                except: pass

            await db.execute("UPDATE tickets SET discord_msg_id = ?, status = 'PENDING' WHERE discord_msg_id = ?", (new_msg.id, interaction.message.id))
            await db.commit()
            await interaction.message.delete()
            await interaction.followup.send("🔙 تم إعادة التذكرة لقناة (قيد الانتظار).", ephemeral=True)

    @discord.ui.button(label="إغلاق إجباري ❌", style=discord.ButtonStyle.danger, custom_id="btn_suspend_force_close")
    async def force_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles):
            await interaction.response.send_message("❌ للإدارة فقط.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT github_issue_num, thread_id FROM tickets WHERE discord_msg_id = ?", (interaction.message.id,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    issue_num, thread_id = row
                    await GitHubAPI.close_issue(issue_num)
                    if thread_id:
                        try:
                            thread = interaction.guild.get_thread(thread_id)
                            if thread: await thread.edit(archived=True, locked=True)
                        except Exception: pass
                    await db.execute("DELETE FROM tickets WHERE discord_msg_id = ?", (interaction.message.id,))
                    await db.commit()
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
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("DELETE FROM tickets WHERE discord_msg_id = ?", (interaction.message.id,))
            await db.commit()
        await interaction.message.delete()
        await interaction.response.send_message("📁 تم نقل التذكرة للأرشيف.", ephemeral=True)

# --------------------------------------------------------
# 🔘 الأزرار الثابتة الرئيسية (للتذاكر النشطة)
# --------------------------------------------------------
class PersistentTicketOpsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="تعيين مهام 👤", style=discord.ButtonStyle.primary, custom_id="btn_global_assign")
    async def assign_dev(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("اختر العضو من القائمة:", view=AssigneeSelectView(interaction.message.id), ephemeral=True)

    @discord.ui.button(label="تعليق ⏸️", style=discord.ButtonStyle.secondary, custom_id="btn_global_suspend")
    async def suspend_task(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SuspendModal(interaction.message.id))

    @discord.ui.button(label="إنهاء المهمة ✅", style=discord.ButtonStyle.success, custom_id="btn_mark_done")
    async def mark_done(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT github_issue_num, thread_id, title, description, ticket_type, assignee_id, dod FROM tickets WHERE discord_msg_id = ?", (interaction.message.id,)) as cursor:
                row = await cursor.fetchone()
                if not row: return
                issue_num, thread_id, title, desc, t_type, assignee_id, dod = row

            is_admin = any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles)
            if not is_admin and interaction.user.id != assignee_id:
                await interaction.followup.send("❌ فقط المطور المسؤول أو الإدارة يحق لهم إنهاء هذه المهمة.", ephemeral=True)
                return

            await GitHubAPI.close_issue(issue_num)
            done_channel = bot.get_channel(DONE_CHANNEL_ID)
            
            done_embed = discord.Embed(
                title=f"🎉 #{issue_num} | {t_type} | {title}", 
                description=f"### الوصف:\n{desc}\n\n### 🎯 متطلبات الإنهاء (DoD):\n{dod}", 
                color=discord.Color.dark_theme()
            )
            
            new_msg = await done_channel.send(embed=done_embed, view=DoneTicketOpsView())

            if thread_id:
                try:
                    thread = interaction.guild.get_thread(thread_id)
                    if thread:
                        await thread.send("🔒 **مكتمل:** تم إنهاء المهمة وقفل الغرفة بنجاح.")
                        await thread.edit(archived=True, locked=True)
                except Exception: pass

            await db.execute("UPDATE tickets SET discord_msg_id = ?, status = 'DONE' WHERE discord_msg_id = ?", (new_msg.id, interaction.message.id))
            await db.commit()
            await interaction.message.delete()
            await interaction.followup.send("✅ تم اعتماد التذكرة ونقلها لقناة المكتملة.", ephemeral=True)

    @discord.ui.button(label="إغلاق إجباري ❌", style=discord.ButtonStyle.danger, custom_id="btn_global_force_close")
    async def force_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles):
            await interaction.response.send_message("❌ للإدارة فقط.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT github_issue_num, thread_id FROM tickets WHERE discord_msg_id = ?", (interaction.message.id,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    issue_num, thread_id = row
                    await GitHubAPI.close_issue(issue_num)
                    if thread_id:
                        try:
                            thread = interaction.guild.get_thread(thread_id)
                            if thread: await thread.edit(archived=True, locked=True)
                        except Exception: pass
                    await db.execute("DELETE FROM tickets WHERE discord_msg_id = ?", (interaction.message.id,))
                    await db.commit()
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
                async with aiosqlite.connect(DB_FILE) as db:
                    await db.execute("DELETE FROM tickets WHERE discord_msg_id = ?", (message.id,))
                    await db.commit()
                await message.delete()

# --------------------------------------------------------
# 🚀 الإقـلاع
# --------------------------------------------------------
@bot.event
async def on_ready():
    await init_db()
    bot.add_view(InitialTicketBootstrapView())
    bot.add_view(PersistentTicketOpsView())
    bot.add_view(DoneTicketOpsView())
    bot.add_view(SuspendedTicketOpsView()) # إضافة الأزرار الجديدة للذاكرة
    
    if not archive_old_tickets_task.is_running(): archive_old_tickets_task.start()
    bot.loop.create_task(fake_web_server())
    print(f"🔥 النظام المدمّر أونلاين ومستعد للعمل. البوت: {bot.user}")

@bot.command()
@commands.has_permissions(administrator=True)
async def setup_tickets(ctx):
    embed = discord.Embed(title="💼 بوابة إدارة مهام فريق Gestax", description="اضغط لفتح تذكرة جديدة. سيُطلب منك تحديد نوع المهمة (برمجية، تصميم، إلخ).", color=discord.Color.blurple())
    await ctx.send(embed=embed, view=InitialTicketBootstrapView())
    await ctx.message.delete()

bot.run(TOKEN)