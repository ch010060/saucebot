import asyncio
import logging
import re
import reprlib
import typing
from io import BytesIO

import discord
import pysaucenao
from discord.embeds import EmptyEmbed
from discord.ext import commands
from pysaucenao import DailyLimitReachedException, GenericSource, InvalidImageException, InvalidOrWrongApiKeyException, \
    MangaSource, SauceNao, SauceNaoException, ShortLimitReachedException, VideoSource
from pysaucenao.containers import ACCOUNT_ENHANCED, AnimeSource, BooruSource

import saucebot.assets
from saucebot.bot import bot
from saucebot.config import config, server_api_limit
from saucebot.helpers import basic_embed, keycap_emoji, keycap_to_int, reaction_check, validate_url
from saucebot.lang import lang
from saucebot.models.database import SauceCache, SauceQueries, Servers
from saucebot.tracemoe import ATraceMoe


# noinspection PyMethodMayBeStatic
class Sauce(commands.Cog):
    """
    SauceNao commands
    """

    IMAGE_URL_RE = re.compile(r"^https?://\S+(\.jpg|\.png|\.jpeg|\.webp)$")

    def __init__(self):
        self._log = logging.getLogger(__name__)
        self._api_key = config.get('SauceNao', 'api_key', fallback=None)
        self._re_api_key = re.compile(r"^[a-zA-Z0-9]{40}$")
        self.tracemoe = None

        bot.loop.create_task(self.purge_cache())
        self.ready_tracemoe()

    def ready_tracemoe(self):
        token = config.get('TraceMoe', 'token', fallback=None)
        if token:
            self.tracemoe = ATraceMoe(bot.loop, token)

    @commands.command(aliases=['source'])
    @commands.cooldown(server_api_limit or 10000, 86400, commands.BucketType.guild)
    async def sauce(self, ctx: commands.Context, url: typing.Optional[str] = None) -> None:

        # Check channel id and message author at first
        author_id = ctx.message.author.id
        channel_id = ctx.channel.id
        target_channel_id = config.get('Discord', 'channel_id', fallback=None)
        if str(channel_id) not in target_channel_id or author_id == bot.user.id:
            return

        """
        Get the source of the attached image, the image in the message you replied to, the specified image URL,
        or the last image uploaded to the channel if none of these are supplied
        """
        # No URL specified? Check for attachments.
        image_in_command = bool(url) or bool(self._get_image_attachments(ctx.message))

        # Next, check and see if we're replying to a message
        if ctx.message.reference and not image_in_command:
            reference = ctx.message.reference.resolved
            self._log.debug(f"Message reference in command: {reference}")
            if isinstance(reference, discord.Message):
                image_attachments = self._get_image_attachments(reference)
                if image_attachments:
                    if len(image_attachments) > 1:
                        attachment = await self._index_prompt(ctx, ctx.channel, image_attachments)
                        url = attachment.url
                    else:
                        url = image_attachments[0].url

            # If we passed a reference and found nothing, we should abort now
            if not url:
                await ctx.reply(
                    embed=basic_embed(
                        title=lang('Global', 'generic_error'),
                        description=lang('Sauce', 'no_images'),
                        avatar=saucebot.assets.AVATAR_SILLY
                    )
                )
                return

        # Lastly, if all else fails, search for the last message in the channel with an image upload
        url = url or await self._get_last_image_post(ctx)

        # Still nothing? We tried everything we could, exit with an error
        if not url:
            await ctx.reply(
                embed=basic_embed(
                    title=lang('Global', 'generic_error'),
                    description=lang('Sauce', 'no_images'),
                    avatar=saucebot.assets.AVATAR_SILLY
                )
            )
            return

        self._log.info(f"[{ctx.guild.name}] Looking up image source/sauce: {url}")

        # Make sure the URL is valid
        if not validate_url(url):
            await ctx.reply(
                embed=basic_embed(
                    title=lang('Global', 'generic_error'),
                    description=lang('Sauce', 'bad_url'),
                    avatar=saucebot.assets.AVATAR_SILLY
                )
            )
            return

        # Make sure this user hasn't exceeded their API limits
        if self._check_member_limited(ctx):
            await ctx.reply(
                embed=basic_embed(
                    title=lang('Global', 'generic_error'),
                    description=lang('Sauce', 'member_api_limit_exceeded'),
                    avatar=saucebot.assets.AVATAR_SILLY
                )
            )
            return

        # Attempt to find the source of this image
        try:
            preview = None
            sauce = await self._get_sauce(ctx, url)
        except (ShortLimitReachedException, DailyLimitReachedException):
            await ctx.message.delete()
            await ctx.reply(
                embed=basic_embed(
                    title=lang('Global', 'generic_error'),
                    description=lang('Sauce', 'api_limit_exceeded'),
                    avatar=saucebot.assets.AVATAR_THINKING
                )
            )
            return
        except InvalidOrWrongApiKeyException:
            self._log.warning(f"[{ctx.guild.name}] API key was rejected by SauceNao")
            await ctx.message.delete()
            await ctx.reply(
                embed=basic_embed(
                    title=lang('Global', 'generic_error'),
                    description=lang('Sauce', 'rejected_api_key'),
                    avatar=saucebot.assets.AVATAR_THINKING
                )
            )
            return
        except InvalidImageException:
            self._log.info(f"[{ctx.guild.name}] An invalid image / image link was provided")
            await ctx.message.delete()
            await ctx.reply(
                embed=basic_embed(
                    title=lang('Global', 'generic_error'),
                    description=lang('Sauce', 'no_images'),
                    avatar=saucebot.assets.AVATAR_SILLY
                )
            )
            return
        except SauceNaoException:
            self._log.exception(f"[{ctx.guild.name}] An unknown error occurred while looking up this image")
            await ctx.message.delete()
            await ctx.reply(
                embed=basic_embed(
                    title=lang('Global', 'generic_error'),
                    description=lang('Sauce', 'api_offline'),
                    avatar=saucebot.assets.AVATAR_THINKING
                )
            )
            return

        # If it's an anime, see if we can find a preview clip
        if isinstance(sauce, AnimeSource):
            preview_file, nsfw = await self._video_preview(sauce, url, True)
            if preview_file:
                if nsfw and not ctx.channel.is_nsfw():
                    self._log.info(f"Channel #{ctx.channel.name} is not NSFW; not uploading an NSFW video here")
                else:
                    preview = discord.File(
                            BytesIO(preview_file),
                            filename=f"{sauce.title}_preview.mp4".lower().replace(' ', '_')
                    )

        # We didn't find anything, provide some suggestions for manual investigation
        if not sauce:
            self._log.info(f"[{ctx.guild.name}] No image sources found")
            embed = basic_embed(
                title=lang('Sauce', 'not_found', member=ctx.author),
                description=lang('Sauce', 'not_found_advice'),
                avatar=saucebot.assets.AVATAR_THINKING
            )

            google_url  = f"https://www.google.com/searchbyimage?sbisrc=4chanx&image_url={url}&safe=off"
            ascii_url   = f"https://ascii2d.net/search/url/{url}"
            yandex_url  = f"https://yandex.com/images/search?url={url}&rpt=imageview"
            iqdb_url    = f"https://iqdb.org/?url={url}"

            urls = [
                (lang('Sauce', 'google'), google_url),
                (lang('Sauce', 'ascii2d'), ascii_url),
                (lang('Sauce', 'yandex'), yandex_url),
                (lang('Sauce', 'iqdb'), iqdb_url)
            ]
            urls = ' • '.join([f"[{t}]({u})" for t, u in urls])

            embed.add_field(name=lang('Sauce', 'search_engines'), value=urls)
            await ctx.reply(embed=embed)
            return

        await ctx.reply(embed=await self._build_sauce_embed(ctx, sauce), file=preview)

        # Only delete the command message if it doesn't contain the image we just looked up
        if not image_in_command and not ctx.message.reference:
            await ctx.message.delete()

    async def _get_last_image_post(self, ctx: commands.Context) -> typing.Optional[str]:
        """
        Get the most recently posted image in this channel
        Args:
            ctx (commands.Context):

        Returns:
            typing.Optional[str]
        """
        async for message in ctx.channel.history(limit=50):  # type: discord.Message
            # Do we have any image or video attachments?
            image_attachments = self._get_image_attachments(message)

            if image_attachments:
                if len(image_attachments) > 1:
                    attachment = await self._index_prompt(ctx, ctx.channel, image_attachments)
                else:
                    attachment = image_attachments[0]

                image_url = self._get_attachment_image(attachment)
                self._log.info(f"[{ctx.guild.name}] Attachment found: {image_url}")
                return image_url

            # How about a valid image link?
            if self.IMAGE_URL_RE.match(message.content):
                self._log.debug(f"[{ctx.guild.name}] Message contains an embedded image link: {message.content}")
                return message.content

    def _get_image_attachments(self, message: discord.Message) -> typing.Optional[typing.List[discord.Attachment]]:
        """
        Gets all image attachments associated with an image.
        Args:
            message (discord.Message): The message to check.

        Returns:
            list
        """
        image_attachments = []
        for attachment in message.attachments:  # type: discord.Attachment
            # Native images
            if self._get_attachment_image(attachment):
                image_attachments.append(attachment)

        self._log.debug(f"Found {len(image_attachments)} image(s) in message {message.id}")
        return image_attachments

    def _get_attachment_image(self, attachment: discord.Attachment) -> typing.Optional[str]:
        """
        Gets the image associated with an attachment, including support for video attachments
        Args:
            attachment (discord.Attachment): The attachment to process.

        Returns:
            typing.Optional[str]
        """
        if not attachment.url:
            return None

        if attachment.url and str(attachment.url).endswith(('.jpg', '.png', '.gif', '.jpeg', '.webp')):
            return attachment.url

        if attachment.url and str(attachment.url).endswith(('.mp4', '.webm', '.mov')):
            return attachment.proxy_url + '?format=jpeg'

    async def _index_prompt(self, ctx: commands.Context, channel: discord.TextChannel, items: list):
        prompt = await channel.send(lang('Sauce', 'multiple_images'))  # type: discord.Message
        index_range = range(1, min(len(items), 10) + 1)

        # Add the numerical emojis. The syntax is weird for this.
        for index in index_range:
            await prompt.add_reaction(keycap_emoji(index))

        try:
            check = reaction_check(prompt, [ctx.message.author.id], [keycap_emoji(i) for i in index_range])
            reaction, user = await ctx.bot.wait_for('reaction_add', timeout=60.0, check=check)
        except asyncio.TimeoutError:
            await ctx.message.delete()
            await prompt.delete()
            return

        await prompt.delete()
        return items[keycap_to_int(reaction) - 1]

    async def _get_sauce(self, ctx: commands.Context, url: str) -> typing.Optional[GenericSource]:
        """
        Perform a SauceNao lookup on the supplied URL
        Args:
            ctx (commands.Context):
            url (str):

        Returns:
            typing.Optional[GenericSource]
        """
        # Get the API key for this server
        api_key = Servers.lookup_guild(ctx.guild)
        if not api_key:
            api_key = self._api_key

        # Log the query
        SauceQueries.log(ctx, url)

        cache = SauceCache.fetch(url)  # type: SauceCache
        if cache:
            container   = getattr(pysaucenao.containers, cache.result_class)
            sauce       = container(cache.header, cache.result)  # type: GenericSource
            self._log.info(f'Cache entry found: {sauce.title}')
        else:
            # Initialize SauceNao and execute a search query
            saucenao = SauceNao(api_key=api_key,
                                min_similarity=float(config.get('SauceNao', 'min_similarity', fallback=50.0)),
                                priority=[21, 22, 5, 37, 25])
            search = await saucenao.from_url(url)
            sauce = search.results[0] if search.results else None

            # Log output
            rep = reprlib.Repr()
            rep.maxstring = 16
            self._log.debug(
                f"[{ctx.guild.name}] {search.short_remaining} short API queries remaining for {rep.repr(api_key)}"
            )
            self._log.info(
                f"[{ctx.guild.name}] {search.long_remaining} daily API queries remaining for {rep.repr(api_key)}"
            )

            # Cache the search result
            if sauce:
                SauceCache.add_or_update(url, sauce)

        return sauce

    async def _build_sauce_embed(self, ctx: commands.Context, sauce: GenericSource) -> discord.Embed:
        """
        Builds a Discord embed for the provided SauceNao lookup
        Args:
            ctx (commands.Context)
            sauce (GenericSource):

        Returns:
            discord.Embed
        """
        embed = basic_embed()
        embed.set_footer(text=lang('Sauce', 'found', member=ctx.author), icon_url=saucebot.assets.ICON_FOOTER)
        embed.title = sauce.title or sauce.author_name or "Untitled"
        embed.url = sauce.url
        if embed.url and "illust_id" in embed.url:
            embed.url = embed.url.replace("member_illust.php?mode=medium&illust_id=","artworks/")
        embed.description = lang('Sauce', 'match_title', {'index': sauce.index, 'similarity': sauce.similarity})

        # For low similarity results, tweak our response a bit
        if sauce.similarity <= 60:
            embed.set_thumbnail(url=saucebot.assets.AVATAR_THINKING)
            embed.description = lang('Sauce', 'match_title', {'index': sauce.index, 'similarity': sauce.similarity})
            embed.set_footer(text=lang('Sauce', 'found_low_confidence', member=ctx.author), icon_url=saucebot.assets.ICON_FOOTER)

        if sauce.author_name and sauce.title:
            embed.set_author(name=sauce.author_name, url=sauce.author_url or EmptyEmbed)
        embed.set_image(url=sauce.thumbnail)

        if isinstance(sauce, VideoSource):
            embed.add_field(name=lang('Sauce', 'episode'), value=sauce.episode)
            embed.add_field(name=lang('Sauce', 'timestamp'), value=sauce.timestamp)

        if isinstance(sauce, AnimeSource):
            await sauce.load_ids()
            urls = [(lang('Sauce', 'anidb'), sauce.anidb_url)]

            if sauce.mal_url:
                urls.append((lang('Sauce', 'mal'), sauce.mal_url))

            if sauce.anilist_url:
                urls.append((lang('Sauce', 'anilist'), sauce.anilist_url))

            urls = ' • '.join([f"[{t}]({u})" for t, u in urls])
            embed.add_field(name=lang('Sauce', 'more_info'), value=urls, inline=False)

        if isinstance(sauce, MangaSource):
            embed.add_field(name=lang('Sauce', 'chapter'), value=sauce.chapter)

        if isinstance(sauce, BooruSource):
            if sauce.characters:
                characters = [c.title() for c in sauce.characters]
                embed.add_field(name=lang('Sauce', 'characters'), value=', '.join(characters), inline=False)
            if sauce.material:
                material = [m.title() for m in sauce.material]
                embed.add_field(name=lang('Sauce', 'material'), value=', '.join(material), inline=False)

        return embed

    async def _video_preview(self, sauce: AnimeSource, path_or_fh: typing.Union[str, typing.BinaryIO],
                             is_url: bool) -> typing.Tuple[typing.Optional[bytes], bool]:
        """
        Attempt to grab a video preview of an AnimeSource entry from trace.moe
        Args:
            sauce (AnimeSource): An anime of hentai source
            path_or_fh (typing.Union[str, typing.BinaryIO]): Path or file handler
            is_url (bool): Path is a URL to an image rather than a file path.

        Returns:
            typing.Tuple[typing.Optional[bytes], bool]: The video preview if available, and whether the video is NSFW.
        """
        if not self.tracemoe:
            return None, False

        # noinspection PyBroadException
        try:
            tracemoe_sauce = await self.tracemoe.search(path_or_fh, is_url=is_url)
            if not tracemoe_sauce.get('docs'):
                self._log.info("Tracemoe returned no results")
                return None, False
        except Exception as e:
            self._log.error(f"Tracemoe returned an exception: {e}")
            return None, False

        # Make sure our search results match
        if await sauce.load_ids():
            if sauce.anilist_id != tracemoe_sauce['docs'][0]['anilist_id']:
                self._log.info(f"saucenao and trace.moe provided mismatched anilist entries: "
                               f"`{sauce.anilist_id}` vs. `{tracemoe_sauce['docs'][0]['anilist_id']}`")
                return None, False

            self._log.info(f'Downloading video preview for AniList entry {sauce.anilist_id} from trace.moe')
            tracemoe_preview = await self.tracemoe.video_preview_natural(tracemoe_sauce)
            return tracemoe_preview, tracemoe_sauce['docs'][0]['is_adult']

        return None, False

    @sauce.error
    async def sauce_error(self, ctx: commands.Context, error) -> None:
        """
        Override guild cooldowns for servers with their own API keys provided
        Args:
            ctx (commands.Context):
            error (Exception):

        Returns:
            None
        """
        if isinstance(error, commands.CommandOnCooldown):
            if Servers.lookup_guild(ctx.guild):
                self._log.info(f"[{ctx.guild.name}] Guild has an enhanced API key; ignoring triggered guild API limit")
                await ctx.reinvoke()
                return

            self._log.info(f"[{ctx.guild.name}] Guild has exceeded their available API queries for the day")
            await ctx.send(
                embed=basic_embed(
                    title=lang('Global', 'generic_error'),
                    description=lang('Sauce', 'api_limit_exceeded')
                )
            )

        raise error

    def _check_member_limited(self, ctx: commands.Context) -> bool:
        """
        Check if the author of this message has exceeded their API limits
        Args:
            ctx (commands.Context):

        Returns:
            bool
        """
        member_limit = config.getint('SauceNao', 'member_api_limit', fallback=0)
        if not member_limit:
            self._log.debug('No member limit defined')
            return False

        count = SauceQueries.user_count(ctx.author)
        return count >= member_limit

    @commands.command()
    @commands.has_permissions(administrator=True)
    @commands.cooldown(5, 1800, commands.BucketType.guild)
    async def apikey(self, ctx: commands.Context, api_key: str) -> None:
        """
        Define your own enhanced SauceNao API key for this server.

        This can only be used to add enhanced / upgraded API keys, not freely registered ones. Adding your own enhanced
        API key will remove the shared daily API query limit from your server.

        You can get an enhanced API key from the following page:
        https://saucenao.com/user.php?page=account-upgrades
        """
        await ctx.message.delete()

        # Make sure the API key is formatted properly
        if not self._re_api_key.match(api_key):
            await ctx.send(
                embed=basic_embed(
                    title=lang('Global', 'generic_error'),
                    description=lang('Sauce', 'bad_api_key')
                )
            )
            return

        # Test and make sure it's a valid enhanced-level API key
        saucenao = SauceNao(api_key=api_key)
        test = await saucenao.test()

        # Make sure the test went through successfully
        if not test.success:
            self._log.error(
                f"[{ctx.guild.name}] An unknown error occurred while assigning an API key to this server",
                exc_info=test.error
            )
            await ctx.send(
                embed=basic_embed(
                    title=lang('Global', 'generic_error'),
                    description=lang('Sauce', 'api_offline')
                )
            )
            return

        # Make sure this is an enhanced API key
        if test.account_type != ACCOUNT_ENHANCED:
            self._log.info(f"[{ctx.guild.name}] Rejecting an attempt to register a free API key")
            await ctx.send(
                embed=basic_embed(
                    title=lang('Global', 'generic_error'),
                    description=lang('Sauce', 'api_free')
                )
            )
            return

        Servers.register(ctx.guild, api_key)
        await ctx.send(
            embed=basic_embed(
                title=lang('Global', 'generic_success'),
                description=lang('Sauce', 'registered_api_key')
            )
        )

    # noinspection PyBroadException
    async def purge_cache(self):
        """
        Task to purge SauceNao cache entries older than 24-hours every 6-hours
        Returns:
            None
        """
        await bot.wait_until_ready()

        while not bot.is_closed():
            try:
                self._log.info('[SYSTEM] Purging SauceNao query cache')
                SauceCache.purge_cache()
                await asyncio.sleep(21600)
            except Exception:
                self._log.exception('An unknown error occurred while purging the local query cache')
