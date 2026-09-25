"""
welcome.py — JoinDev welcome DM embed
Edit the copy here without touching bot.py
"""

import discord


def build_welcome_embed() -> discord.Embed:
    """
    Returns the welcome embed sent to users after OAuth authorization.
    """
    embed = discord.Embed(
        title="👋 Welcome to JoinDev!",
        description="*Server growth made easy!*",
        color=0x5865F2,  # Discord blurple
    )

    embed.add_field(
        name="🪙 What Are JoinCoins?",
        value=(
            "JoinCoins are our special currency — **1 JoinCoin = 1 member**.\n"
            "You received **10 free JoinCoins** just for authorizing me!"
        ),
        inline=False,
    )

    embed.add_field(
        name="📅 Daily Rewards",
        value=(
            "Run `/daily` in our DMs to earn **3 JoinCoins every 24 hours**.\n"
            "Keep your streak alive and earn **+1 extra JoinCoin per consecutive day**.\n"
            "*Example: Day 1 = 3 coins. Day 2 = 4 coins. Day 3 = 5 coins.*"
        ),
        inline=False,
    )

    embed.add_field(
        name="🚀 Start Farming",
        value=(
            "Use `/auto_join` and select how many JoinCoins you want to spend — "
            "or type `max` to join as many servers as Discord allows."
        ),
        inline=False,
    )

    embed.add_field(
        name="👥 Get Members",
        value=(
            "Use `/buy_members` and choose how many members you can afford — "
            "or type `max` to spend everything you've got."
        ),
        inline=False,
    )

    embed.set_footer(text="JoinDev • Beta • Server growth made easy!")

    return embed
