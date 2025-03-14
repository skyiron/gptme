import errno
import logging
import os
import re
import sys
import termios
import urllib.parse
from collections.abc import Generator
from pathlib import Path
from typing import cast

from .commands import action_descriptions, execute_cmd
from .config import get_config
from .constants import INTERRUPT_CONTENT, PROMPT_USER
from .init import init
from .llm import reply
from .llm.models import get_model
from .logmanager import Log, LogManager, prepare_messages
from .message import Message
from .prompts import get_workspace_prompt
from .tools import (
    ConfirmFunc,
    ToolFormat,
    ToolUse,
    execute_msg,
    get_tools,
    has_tool,
    set_tool_format,
)
from .tools.browser import read_url
from .tools.tts import speak
from .util import console, path_with_tilde, print_bell
from .util.ask_execute import ask_execute
from .util.context import run_precommit_checks, use_fresh_context
from .util.cost import log_costs
from .util.interrupt import clear_interruptible, set_interruptible
from .util.prompt import add_history, get_input

logger = logging.getLogger(__name__)


def chat(
    prompt_msgs: list[Message],
    initial_msgs: list[Message],
    logdir: Path,
    model: str | None,
    stream: bool = True,
    no_confirm: bool = False,
    interactive: bool = True,
    show_hidden: bool = False,
    workspace: Path | None = None,
    tool_allowlist: list[str] | None = None,
    tool_format: ToolFormat | None = None,
) -> None:
    """
    Run the chat loop.

    prompt_msgs: list of messages to execute in sequence.
    initial_msgs: list of history messages.
    workspace: path to workspace directory, or @log to create one in the log directory.

    Callable from other modules.
    """
    # init
    init(model, interactive, tool_allowlist)

    if not get_model().supports_streaming and stream:
        logger.info(
            "Disabled streaming for '%s/%s' model (not supported)",
            get_model().provider,
            get_model().model,
        )
        stream = False

    console.log(f"Using logdir {path_with_tilde(logdir)}")
    manager = LogManager.load(logdir, initial_msgs=initial_msgs, create=True)

    config = get_config()
    tool_format_with_default: ToolFormat = tool_format or cast(
        ToolFormat, config.get_env("TOOL_FORMAT", "markdown")
    )

    # By defining the tool_format at the last moment we ensure we can use the
    # configuration for subagent
    set_tool_format(tool_format_with_default)

    # change to workspace directory
    # use if exists, create if @log, or use given path
    # TODO: move this into LogManager? then just os.chdir(manager.workspace)
    log_workspace = logdir / "workspace"
    if log_workspace.exists():
        assert not workspace or (
            workspace == log_workspace
        ), f"Workspace already exists in {log_workspace}, wont override."
        workspace = log_workspace.resolve()
    else:
        if not workspace:
            workspace = Path.cwd()
            log_workspace.symlink_to(workspace, target_is_directory=True)
        assert workspace.exists(), f"Workspace path {workspace} does not exist"
    console.log(f"Using workspace at {path_with_tilde(workspace)}")
    os.chdir(workspace)

    workspace_prompt = get_workspace_prompt(workspace)
    # FIXME: this is hacky
    # NOTE: needs to run after the workspace is set
    # check if message is already in log, such as upon resume
    if (
        workspace_prompt
        and workspace_prompt not in [m.content for m in manager.log]
        and "user" not in [m.role for m in manager.log]
    ):
        manager.append(Message("system", workspace_prompt, hide=True, quiet=True))

    # print log
    manager.log.print(show_hidden=show_hidden)
    console.print("--- ^^^ past messages ^^^ ---")

    def confirm_func(msg) -> bool:
        if no_confirm:
            return True
        return ask_execute(msg)

    # main loop
    while True:
        # if prompt_msgs given, process each prompt fully before moving to the next
        if prompt_msgs:
            while prompt_msgs:
                msg = prompt_msgs.pop(0)
                if not msg.content.startswith("/") and msg.role == "user":
                    msg = _include_paths(msg, workspace)
                manager.append(msg)
                # if prompt is a user-command, execute it
                if msg.role == "user" and execute_cmd(msg, manager, confirm_func):
                    continue

                # Generate and execute response for this prompt
                while True:
                    try:
                        set_interruptible()
                        response_msgs = list(
                            step(
                                manager.log,
                                stream,
                                confirm_func,
                                tool_format=tool_format_with_default,
                                workspace=workspace,
                            )
                        )
                    except KeyboardInterrupt:
                        console.log("Interrupted. Stopping current execution.")
                        manager.append(Message("system", INTERRUPT_CONTENT))
                        break
                    finally:
                        clear_interruptible()

                    for response_msg in response_msgs:
                        manager.append(response_msg)
                        # run any user-commands, if msg is from user
                        if response_msg.role == "user" and execute_cmd(
                            response_msg, manager, confirm_func
                        ):
                            break

                    # Check if there are any runnable tools left
                    last_content = next(
                        (
                            m.content
                            for m in reversed(manager.log)
                            if m.role == "assistant"
                        ),
                        "",
                    )
                    has_runnable = any(
                        tooluse.is_runnable
                        for tooluse in ToolUse.iter_from_content(last_content)
                    )
                    if not has_runnable:
                        break

            # All prompts processed, continue to next iteration
            continue

        # if:
        #  - prompts exhausted
        #  - non-interactive
        #  - no executable block in last assistant message
        # then exit
        elif not interactive:
            logger.debug("Non-interactive and exhausted prompts, exiting")
            break

        # ask for input if no prompt, generate reply, and run tools
        clear_interruptible()  # Ensure we're not interruptible during user input
        for msg in step(
            manager.log,
            stream,
            confirm_func,
            tool_format=tool_format_with_default,
            workspace=workspace,
        ):  # pragma: no cover
            manager.append(msg)
            # run any user-commands, if msg is from user
            if msg.role == "user" and execute_cmd(msg, manager, confirm_func):
                break


def step(
    log: Log | list[Message],
    stream: bool,
    confirm: ConfirmFunc,
    tool_format: ToolFormat = "markdown",
    workspace: Path | None = None,
) -> Generator[Message, None, None]:
    """Runs a single pass of the chat."""
    if isinstance(log, list):
        log = Log(log)

    # Check if we have any recent file modifications, and if so, run lint checks
    if not any(
        tooluse.is_runnable
        for tooluse in ToolUse.iter_from_content(
            next((m.content for m in reversed(log) if m.role == "assistant"), "")
        )
    ):
        # Only check for modifications if the last assistant message has no runnable tools
        if check_for_modifications(log) and (failed_check_message := check_changes()):
            yield Message("system", failed_check_message, quiet=False)
            return

    # If last message was a response, ask for input.
    # If last message was from the user (such as from crash/edited log),
    # then skip asking for input and generate response
    last_msg = log[-1] if log else None
    if (
        not last_msg
        or (last_msg.role in ["assistant"])
        or last_msg.content == INTERRUPT_CONTENT
        or last_msg.pinned
        or not any(role == "user" for role in [m.role for m in log])
    ):  # pragma: no cover
        inquiry = prompt_user()
        msg = Message("user", inquiry, quiet=True)
        msg = _include_paths(msg, workspace)
        yield msg
        log = log.append(msg)

    # generate response and run tools
    try:
        set_interruptible()

        # performs reduction/context trimming, if necessary
        msgs = prepare_messages(log.messages, workspace)

        tools = None
        if tool_format == "tool":
            tools = [t for t in get_tools() if t.is_runnable()]

        # generate response
        msg_response = reply(msgs, get_model().full, stream, tools)
        if os.environ.get("GPTME_COSTS") in ["1", "true"]:
            log_costs(msgs + [msg_response])

        # speak if TTS tool is available
        if has_tool("tts"):
            speak(msg_response.content)

        # log response and run tools
        if msg_response:
            yield msg_response.replace(quiet=True)
            yield from execute_msg(msg_response, confirm)
    finally:
        clear_interruptible()


def prompt_user(value=None) -> str:  # pragma: no cover
    print_bell()
    # Flush stdin to clear any buffered input before prompting
    termios.tcflush(sys.stdin, termios.TCIFLUSH)
    response = ""
    while not response:
        try:
            set_interruptible()
            response = prompt_input(PROMPT_USER, value)
            if response:
                add_history(response)
        except KeyboardInterrupt:
            print("\nInterrupted. Press Ctrl-D to exit.")
        except EOFError:
            print("\nGoodbye!")
            sys.exit(0)
    clear_interruptible()
    return response


def prompt_input(prompt: str, value=None) -> str:  # pragma: no cover
    """Get input using prompt_toolkit with fish-style suggestions."""
    prompt = prompt.strip() + ": "
    if value:
        console.print(prompt + value)
        return value

    return get_input(prompt)


def _find_potential_paths(content: str) -> list[str]:
    """
    Find potential file paths and URLs in a message content.
    Excludes content within code blocks.

    Args:
        content: The message content to search

    Returns:
        List of potential paths/URLs found in the message
    """
    # Remove code blocks to avoid matching paths inside them
    content_no_codeblocks = re.sub(r"```[\s\S]*?```", "", content)

    # List current directory contents for relative path matching
    cwd_files = [f.name for f in Path.cwd().iterdir()]

    paths = []

    def is_path_like(word: str) -> bool:
        """Helper to check if a word looks like a path"""
        return (
            # Absolute/home/relative paths
            any(word.startswith(s) for s in ["/", "~/", "./"])
            # URLs
            or word.startswith("http")
            # Contains slash (for backtick-wrapped paths)
            or "/" in word
            # Files in current directory or subdirectories
            or any(word.split("/", 1)[0] == file for file in cwd_files)
        )

    # First find backtick-wrapped content
    for match in re.finditer(r"`([^`]+)`", content_no_codeblocks):
        word = match.group(1).strip()
        word = word.rstrip("?").rstrip(".").rstrip(",").rstrip("!")
        if is_path_like(word):
            paths.append(word)

    # Then find non-backtick-wrapped words
    # Remove backtick-wrapped content first to avoid double-processing
    content_no_backticks = re.sub(r"`[^`]+`", "", content_no_codeblocks)
    for word in re.split(r"\s+", content_no_backticks):
        word = word.strip()
        word = word.rstrip("?").rstrip(".").rstrip(",").rstrip("!")
        if not word:
            continue

        if is_path_like(word):
            paths.append(word)

    return paths


def _include_paths(msg: Message, workspace: Path | None = None) -> Message:
    """
    Searches the message for any valid paths and:
     - In legacy mode (default):
       - includes the contents of text files as codeblocks
       - includes images as msg.files
     - In fresh context mode (GPTME_FRESH_CONTEXT=1):
       - breaks the append-only nature of the log, but ensures we include fresh file contents
       - includes all files in msg.files
       - contents are applied right before sending to LLM (only paths stored in the log)

    Args:
        msg: Message to process
        workspace: If provided, paths will be stored relative to this directory
    """
    # TODO: add support for directories?
    assert msg.role == "user"

    append_msg = ""
    files = []

    # Find potential paths in message
    for word in _find_potential_paths(msg.content):
        logger.debug(f"potential path/url: {word=}")
        # If not using fresh context, include text file contents in the message
        if not use_fresh_context() and (contents := _parse_prompt(word)):
            append_msg += "\n\n" + contents
        else:
            # if we found an non-text file, include it in msg.files
            file = _parse_prompt_files(word)
            if file:
                # Store path relative to workspace if provided
                file = file.expanduser()
                if workspace and not file.is_absolute():
                    file = file.absolute().relative_to(workspace)
                files.append(file)

    if files:
        msg = msg.replace(files=msg.files + files)

    # append the message with the file contents
    if append_msg:
        msg = msg.replace(content=msg.content + append_msg)

    return msg


def _parse_prompt(prompt: str) -> str | None:
    """
    Takes a string that might be a path or URL,
    and if so, returns the contents of that file wrapped in a codeblock.
    """
    # if prompt is a command, exit early (as commands might take paths as arguments)
    if any(
        prompt.startswith(command)
        for command in [f"/{cmd}" for cmd in action_descriptions.keys()]
    ):
        return None

    try:
        # check if prompt is a path, if so, replace it with the contents of that file
        f = Path(prompt).expanduser()
        if f.exists() and f.is_file():
            return f"```{prompt}\n{f.read_text()}\n```"
    except OSError as oserr:
        # some prompts are too long to be a path, so we can't read them
        if oserr.errno != errno.ENAMETOOLONG:
            pass
        raise
    except UnicodeDecodeError:
        # some files are not text files (images, audio, PDFs, binaries, etc), so we can't read them
        # TODO: but can we handle them better than just printing the path? maybe with metadata from `file`?
        # logger.warning(f"Failed to read file {prompt}: not a text file")
        return None

    # check if any word in prompt is a path or URL,
    # if so, append the contents as a code block
    words = prompt.split()
    paths = []
    urls = []
    for word in words:
        f = Path(word).expanduser()
        if f.exists() and f.is_file():
            paths.append(word)
            continue
        try:
            p = urllib.parse.urlparse(word)
            if p.scheme and p.netloc:
                urls.append(word)
        except ValueError:
            pass

    result = ""
    if paths or urls:
        result += "\n\n"
        if paths:
            logger.debug(f"{paths=}")
        if urls:
            logger.debug(f"{urls=}")
    for path in paths:
        result += _parse_prompt(path) or ""

    if not has_tool("browser"):
        logger.warning("Browser tool not available, skipping URL read")
    else:
        for url in urls:
            try:
                content = read_url(url)
                result += f"```{url}\n{content}\n```"
            except Exception as e:
                logger.warning(f"Failed to read URL {url}: {e}")

    return result


def check_for_modifications(log: Log) -> bool:
    """Check if there are any file modifications in last 3 messages or since last user message."""
    messages_since_user = []
    for m in reversed(log):
        if m.role == "user":
            break
        messages_since_user.append(m)

    # FIXME: this is hacky and unreliable
    has_modifications = any(
        tu.tool in ["save", "patch", "append"]
        for m in messages_since_user[:3]
        for tu in ToolUse.iter_from_content(m.content)
    )
    logger.debug(
        f"Found {len(messages_since_user)} messages since user ({has_modifications=})"
    )
    return has_modifications


def check_changes() -> str | None:
    """Run lint/pre-commit checks after file modifications."""
    return run_precommit_checks()


def _parse_prompt_files(prompt: str) -> Path | None:
    """
    Takes a string that might be a supported file path (image, text, PDF) and returns the path.
    Files added here will either be included inline (legacy mode) or in fresh context (fresh context mode).
    """

    # if prompt is a command, exit early (as commands might take paths as arguments)
    if any(
        prompt.startswith(command)
        for command in [f"/{cmd}" for cmd in action_descriptions.keys()]
    ):
        return None

    try:
        p = Path(prompt).expanduser()
        if not (p.exists() and p.is_file()):
            return None

        # Try to read as text
        try:
            p.read_text()
            return p
        except UnicodeDecodeError:
            # If not text, check if supported binary format
            if p.suffix[1:].lower() in ["png", "jpg", "jpeg", "gif", "pdf"]:
                return p
            return None
    except OSError as oserr:  # pragma: no cover
        # some prompts are too long to be a path, so we can't read them
        if oserr.errno != errno.ENAMETOOLONG:
            return None
        raise
