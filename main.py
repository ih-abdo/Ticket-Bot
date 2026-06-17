import os
import discord
from discord.ext import commands, tasks
import aiohttp
import aiosqlite
import datetime
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")
BOOTSTRAP_CHANNEL_ID = int(os.getenv("CHANNEL_BOOTSTRAP_ID"))
PENDING_CHANNEL_ID = int(os.getenv("CHANNEL_NEW_TICKETS_ID")) # القناة ب: قيد الانتظار
IN_PROGRESS_CHANNEL_ID = int(os.getenv("CHANNEL_IN_PROGRESS_ID")) # القناة ج: قيد العمل
DONE_CHANNEL_ID = int(os.getenv("CHANNEL_DONE_ID"))
ARCHIVE_CHANNEL_ID = int(os.getenv("CHANNEL_ARCHIVE_ID"))
ADMIN_ROLE_ID = int(os.getenv("ROLE_ADMIN_ID"))

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
DB_FILE = "gestax_system.db"

# --------------------------------------------------------
# 🗄️ إعداد قاعدة البيانات (انتبه من فقدانها على رندر المجاني)
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
                description TEXT
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
                    data = await resp.json()
                    return data["number"]
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
# 📝 الاستمارات (Modals)
# --------------------------------------------------------
class TicketCreationModal(discord.ui.Modal, title="فتح تذكرة مهام جديدة"):
    ticket_title = discord.ui.TextInput(label="عنوان المهمة", placeholder="مثال: تصميم واجهة الدفع...")
    ticket_desc = discord.ui.TextInput(label="تفاصيل المهمة المطلوبة", style=discord.TextStyle.long)
    ticket_type = discord.ui.TextInput(
        label="التصنيف (إدارية، برمجية، تصميم، أخرى)", 
        placeholder="اكتب نوع التذكرة هنا...", 
        max_length=20
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        t_type = self.ticket_type.value.strip()

        issue_body = f"Requested by: {interaction.user.name}\n\nDescription:\n{self.ticket_desc.value}"
        issue_num = await GitHubAPI.create_issue(self.ticket_title.value, issue_body, t_type)
        
        if not issue_num:
            await interaction.followup.send("❌ فشل فتح التذكرة في جيتهاب، تأكد من التوكن وصلاحيات المستودع.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title=f"⏳ تذكرة قيد الانتظار #{issue_num} | {t_type}",
            description=f"**العنوان:** {self.ticket_title.value}\n\n**الوصف:** {self.ticket_desc.value}",
            color=discord.Color.gold() # لون أصفر ذهبي للانتظار
        )
        embed.add_field(name="المنشئ", value=interaction.user.mention, inline=True)
        embed.add_field(name="الحالة العامة", value="⏸️ قيد الانتظار (في طابور المهام)", inline=True)
        embed.set_footer(text="Gestax Workflow System")

        pending_channel = bot.get_channel(PENDING_CHANNEL_ID)
        msg = await pending_channel.send(embed=embed, view=PersistentTicketOpsView())
        
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT INTO tickets VALUES (?, ?, ?, NULL, NULL, 'PENDING', ?, ?, ?)",
                (msg.id, issue_num, interaction.user.id, t_type, self.ticket_title.value, self.ticket_desc.value)
            )
            await db.commit()

        await interaction.followup.send("✅ تم تسجيل التذكرة وإرسالها إلى قناة (قيد الانتظار) بنجاح!", ephemeral=True)

class SuspendModal(discord.ui.Modal, title="سبب تعليق التذكرة"):
    reason = discord.ui.TextInput(label="لماذا تريد تعليق العمل؟", style=discord.TextStyle.long)

    def __init__(self, msg_id: int):
        super().__init__()
        self.msg_id = msg_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT github_issue_num, creator_id, ticket_type, title, description, assignee_id FROM tickets WHERE discord_msg_id = ?", (self.msg_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    await interaction.followup.send("❌ خطأ: التذكرة غير موجودة في قاعدة البيانات.", ephemeral=True)
                    return
                issue_num, creator_id, t_type, title, desc, assignee_id = row

                embed = discord.Embed(
                    title=f"🛑 تذكرة معلقة #{issue_num} | {t_type}",
                    description=f"**العنوان:** {title}\n\n**الوصف:** {desc}",
                    color=discord.Color.red() # أحمر للتعليق القسري
                )
                creator_user = interaction.guild.get_member(creator_id)
                assignee_user = interaction.guild.get_member(assignee_id) if assignee_id else None
                
                embed.add_field(name="المنشئ", value=creator_user.mention if creator_user else "غير معروف", inline=True)
                embed.add_field(name="المطور المعين", value=assignee_user.mention if assignee_user else "لم يعين بعد", inline=True)
                embed.add_field(name="⚠️ سبب الإيقاف", value=self.reason.value, inline=False)
                
                await interaction.message.edit(embed=embed)
                await db.execute("UPDATE tickets SET status = 'SUSPENDED' WHERE discord_msg_id = ?", (self.msg_id,))
                await db.commit()
                
        await interaction.followup.send("⏸️ تم تعليق التذكرة وتحديث حالتها.", ephemeral=True)

# --------------------------------------------------------
# 👥 قوائم التعيين والثريدات
# --------------------------------------------------------
class AssigneeSelect(discord.ui.UserSelect):
    def __init__(self, msg_id: int):
        super().__init__(
            custom_id=f"assign_select_menu:{msg_id}", 
            placeholder="اختر العضو لتسليمه هذه المهمة...",
            min_values=1, 
            max_values=1
        )
        self.msg_id = msg_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        assigned_developer = self.values[0]

        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT github_issue_num, creator_id, ticket_type, title, description FROM tickets WHERE discord_msg_id = ?", (self.msg_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    await interaction.followup.send("❌ التذكرة غير موجودة.", ephemeral=True)
                    return
                issue_num, creator_id, t_type, title, desc = row

            in_progress_channel = bot.get_channel(IN_PROGRESS_CHANNEL_ID)
            
            work_embed = discord.Embed(
                title=f"👨‍💻 تذكرة قيد العمل #{issue_num} | {t_type}",
                description=f"**العنوان:** {title}\n\n**الوصف:** {desc}",
                color=discord.Color.blue()
            )
            creator_user = interaction.guild.get_member(creator_id)
            work_embed.add_field(name="المنشئ", value=creator_user.mention if creator_user else "غير معروف", inline=True)
            work_embed.add_field(name="المستلم", value=assigned_developer.mention, inline=True)
            work_embed.add_field(name="الحالة", value="⚙️ جاري العمل في الثريد المخصص", inline=False)
            
            new_msg = await in_progress_channel.send(embed=work_embed, view=PersistentTicketOpsView())

            thread = await in_progress_channel.create_thread(
                name=f"🔒-عمل-تذكرة-{issue_num}",
                type=discord.ChannelType.private_thread,
                auto_archive_duration=1440
            )
            
            await thread.add_user(interaction.user)
            if creator_user:
                await thread.add_user(creator_user)
            await thread.add_user(assigned_developer)
            
            await thread.send(f"⚠️ **مساحة عمل سرية:**\nتم عزل هذه الغرفة لمناقشة التذكرة رقم #{issue_num}.")

            await db.execute(
                "UPDATE tickets SET discord_msg_id = ?, assignee_id = ?, thread_id = ?, status = 'IN_PROGRESS' WHERE discord_msg_id = ?",
                (new_msg.id, assigned_developer.id, thread.id, self.msg_id)
            )
            await db.commit()

            try:
                old_msg = await interaction.channel.fetch_message(self.msg_id)
                await old_msg.delete()
            except Exception:
                pass

        await interaction.followup.send(f"🎯 تم سحب التذكرة من الانتظار وتفعيلها في قناة قيد العمل.", ephemeral=True)

class AssigneeSelectView(discord.ui.View):
    def __init__(self, msg_id: int):
        super().__init__(timeout=60)
        self.add_item(AssigneeSelect(msg_id))

# --------------------------------------------------------
# 🔘 الأزرار الثابتة (Persistent Views)
# --------------------------------------------------------
class PersistentTicketOpsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="تعيين وإرسال للعمل 👤", style=discord.ButtonStyle.primary, custom_id="btn_global_assign")
    async def assign_dev(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("اختر العضو المناسب من القائمة لبدء العمل:", view=AssigneeSelectView(interaction.message.id), ephemeral=True)

    @discord.ui.button(label="تعليق ⏸️", style=discord.ButtonStyle.secondary, custom_id="btn_global_suspend")
    async def suspend_task(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SuspendModal(interaction.message.id))

    @discord.ui.button(label="إغلاق إجباري ❌", style=discord.ButtonStyle.danger, custom_id="btn_global_force_close")
    async def force_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles):
            await interaction.response.send_message("❌ عذراً، هذا الإجراء للإدارة فقط.", ephemeral=True)
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
                    await interaction.followup.send("🗑️ تم حذف التذكرة بالكامل.", ephemeral=True)

class InitialTicketBootstrapView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="فتح تذكرة مهام 📝", style=discord.ButtonStyle.success, custom_id="btn_trigger_modal_creation")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketCreationModal())

# --------------------------------------------------------
# 🎭 نظام الاستماع للـ Reaction (✅) للإنهاء
# --------------------------------------------------------
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if str(payload.emoji) == "✅" and payload.user_id != bot.user.id:
        member = payload.member
        if not member or not any(role.id == ADMIN_ROLE_ID for role in member.roles):
            return

        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT github_issue_num, thread_id, title, description, ticket_type FROM tickets WHERE discord_msg_id = ?", (payload.message_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return
                issue_num, thread_id, title, desc, t_type = row

            await GitHubAPI.close_issue(issue_num)

            done_channel = bot.get_channel(DONE_CHANNEL_ID)
            done_embed = discord.Embed(
                title=f"🎉 مهمة مكتملة #{issue_num}",
                description=f"**العنوان:** {title}\n\n**الوصف:** {desc}",
                color=discord.Color.dark_gray()
            )
            done_embed.add_field(name="القسم", value=t_type, inline=True)
            done_embed.add_field(name="الحالة النهائية", value="✅ تم الإنجاز والاعتماد", inline=True)
            done_embed.set_footer(text=f"Completed at: {datetime.datetime.now().strftime('%Y-%m-%d')}")
            
            await done_channel.send(embed=done_embed)

            if thread_id:
                try:
                    guild = bot.get_guild(payload.guild_id)
                    thread = guild.get_thread(thread_id)
                    if thread:
                        await thread.send("🔒 **مكتمل:** تم إنهاء المهمة وقفل الغرفة.")
                        await thread.edit(archived=True, locked=True)
                except Exception: pass

            try:
                channel = bot.get_channel(payload.channel_id)
                msg = await channel.fetch_message(payload.message_id)
                await msg.delete()
            except Exception: pass

            await db.execute("DELETE FROM tickets WHERE discord_msg_id = ?", (payload.message_id,))
            await db.commit()

# --------------------------------------------------------
# ⏰ الأرشفة الدورية لـ (الأرشيف هـ)
# --------------------------------------------------------
@tasks.loop(hours=24)
async def archive_old_tickets_task():
    done_channel = bot.get_channel(DONE_CHANNEL_ID)
    archive_channel = bot.get_channel(ARCHIVE_CHANNEL_ID)
    
    if not done_channel or not archive_channel:
        return
    
    async_history = done_channel.history(limit=100)
    async for message in async_history:
        now = datetime.datetime.now(datetime.timezone.utc)
        if (now - message.created_at).days >= 2:
            if message.embeds:
                await archive_channel.send(embed=message.embeds[0])
                await message.delete()

# --------------------------------------------------------
# 🚀 الإقـلاع والـتثبيت
# --------------------------------------------------------
@bot.event
async def on_ready():
    await init_db()
    bot.add_view(InitialTicketBootstrapView())
    bot.add_view(PersistentTicketOpsView())
    if not archive_old_tickets_task.is_running():
        archive_old_tickets_task.start()
    print(f"🔥 النظام أونلاين ومستعد للعمل. البوت: {bot.user}")

@bot.command()
@commands.has_permissions(administrator=True)
async def setup_tickets(ctx):
    embed = discord.Embed(
        title="💼 بوابة إدارة مهام الفريق",
        description="اضغط لفتح تذكرة جديدة. ستُرسل التذكرة إلى قسم (قيد الانتظار) لمراجعتها قبل التعيين.",
        color=discord.Color.blurple()
    )
    await ctx.send(embed=embed, view=InitialTicketBootstrapView())
    await ctx.message.delete()

bot.run(TOKEN)