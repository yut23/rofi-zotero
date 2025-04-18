#!/usr/bin/env python
"""Use Rofi to search and open attachments from Zotero.

Requirements:
- Python 3.7 or newer
- Rofi

Author: Hans Chen (contact@hanschen.org)

"""

import argparse
import configparser
import functools
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

__version__ = "0.1"

DEFAULT_ZOTERO_CONFIG = Path.home() / ".zotero"
DEFAULT_ZOTERO_PATH = Path.home() / "Zotero"
DEFAULT_ZOTERO_BASE_DIR = Path.home() / "papers"
DEFAULT_ROFI_ARGS = "-i"  # case insensitive
DEFAULT_VIEWER = "xdg-open %u"
DEFAULT_PROMPT_PAPER = "paper"
DEFAULT_PROMPT_ATTACHMENT = "attachment"

FORMAT_ITEM = "{author} ({year}) - {title}"
FORMAT_ITEM_WITHOUT_YEAR = "{author} - {title}"
FORMAT_ITEM_WITHOUT_AUTHOR = "Unknown ({year}) - {title}"
FORMAT_ITEM_WITHOUT_AUTHOR_AND_YEAR = "Unknown - {title}"

FORMAT_AUTHORS_SINGLE = "{author1}"
FORMAT_AUTHORS_TWO = "{author1} and {author2}"
FORMAT_AUTHORS_MULTIPLE = "{author1} et al."

FORMAT_PATH_MAXLEN = 80
FORMAT_PATH_FULLPATH = False
FORMAT_PATH_ENDING_FRACTION = 0.5

# Zotero directories and files
_ZOTERO_STORAGE_DIR = "storage"
_ZOTERO_SQLITE_FILE = "zotero.sqlite"

# SQL queries for internal use
_QUERY_AUTHORS = """
    SELECT items.itemID,
            creators.Lastname
    FROM    ((creators
              JOIN itemCreators
                ON creators.creatorID = itemCreators.creatorID) AS t1
             JOIN items
               ON t1.itemID = items.itemID)
"""

_QUERY_TITLES = """
    SELECT parentInfo.itemID,
           parentItemDataValues.value,
           attachmentInfo.key
    FROM   itemAttachments
           INNER JOIN items AS attachmentInfo
                   ON attachmentInfo.itemID = itemAttachments.itemID
           INNER JOIN itemData AS attachmentItemData
                   ON attachmentItemData.itemID = attachmentInfo.itemID
           INNER JOIN itemDataValues AS attachmentItemDataValues
                   ON attachmentItemData.valueID =
                      attachmentItemDataValues.valueID
           INNER JOIN items AS parentInfo
                   ON itemAttachments.parentItemID = parentInfo.itemID
           INNER JOIN itemData AS parentItemData
                   ON parentItemData.itemID = parentInfo.itemID
           INNER JOIN itemDataValues AS parentItemDataValues
                   ON parentItemDataValues.valueID = parentItemData.valueID
    WHERE  attachmentItemData.fieldID = 1
           AND parentItemData.fieldID = 1
           AND ( itemAttachments.contentType LIKE '%pdf%'
                 OR itemAttachments.contentType LIKE '%djvu%' )
"""

_QUERY_DATES = """
    SELECT parentInfo.itemID,
           parentItemDataValues.value
    FROM   itemAttachments
           INNER JOIN items AS attachmentInfo
                   ON attachmentInfo.itemID = itemAttachments.itemID
           INNER JOIN itemData AS attachmentItemData
                   ON attachmentItemData.itemID = attachmentInfo.itemID
           INNER JOIN itemDataValues AS attachmentItemDataValues
                   ON attachmentItemData.valueID =
                      attachmentItemDataValues.valueID
           INNER JOIN items AS parentInfo
                   ON itemAttachments.parentItemID = parentInfo.itemID
           INNER JOIN itemData AS parentItemData
                   ON parentItemData.itemID = parentInfo.itemID
           INNER JOIN itemDataValues AS parentItemDataValues
                   ON parentItemDataValues.valueID = parentItemData.valueID
    WHERE parentItemData.fieldID=6
"""

_QUERY_PATHS = """
    SELECT parentInfo.itemID,
           path,
           attachmentItemDataValues.value
    FROM   itemAttachments
           INNER JOIN items AS attachmentInfo
                   ON attachmentInfo.itemID = itemAttachments.itemID
           INNER JOIN itemData AS attachmentItemData
                   ON attachmentItemData.itemID = attachmentInfo.itemID
           INNER JOIN itemDataValues AS attachmentItemDataValues
                   ON attachmentItemData.valueID =
                      attachmentItemDataValues.valueID
           INNER JOIN items AS parentInfo
                   ON itemAttachments.parentItemID = parentInfo.itemID
           INNER JOIN itemData AS parentItemData
                   ON parentItemData.itemID = parentInfo.itemID
           INNER JOIN itemDataValues AS parentItemDataValues
                   ON parentItemDataValues.valueID = parentItemData.valueID
    WHERE  attachmentItemData.fieldID = 1
           AND parentItemData.fieldID = 1
           AND ( itemAttachments.contentType LIKE '%pdf%'
                 OR itemAttachments.contentType LIKE '%djvu%' )
"""

_QUERY_COLLECTIONS = """
    SELECT collectionID,
           collectionName,
           parentCollectionID
    FROM   collections
"""
_QUERY_COLLECTION_ITEMS = """
    SELECT itemID,
           collectionID
    FROM   collectionItems
"""


def construct_collection_paths(
    all_collections: list[tuple[int, str, int | None]]
) -> dict[int, str]:
    lookup = {id_: (name, parent) for id_, name, parent in all_collections}

    @functools.lru_cache(maxsize=None)
    def get_path(id_: int) -> str:
        name, parent = lookup[id_]
        if parent is None:
            return name
        else:
            return get_path(parent) + "/" + name

    return {id_: get_path(id_) for id_ in lookup}


def format_authors(author_list):
    """Format author string depending on the number of authors.

    Parameters
    ----------
    author_list : list
        A list of all authors.

    Returns
    -------
    str
        String of formatted author list.

    """
    if len(author_list) == 1:
        author_string = FORMAT_AUTHORS_SINGLE.format(author1=author_list[0])
    elif len(author_list) == 2:
        author_string = FORMAT_AUTHORS_TWO.format(
            author1=author_list[0], author2=author_list[1]
        )
    else:
        author_string = FORMAT_AUTHORS_MULTIPLE.format(author1=author_list[0])

    return author_string


def format_path(path, maxlen=80, fullpath=False, ending_fraction=0.5):
    """Format path to shorten it.

    Parameters
    ----------
    path : pathlib.Path
        Path to format.
    maxlen : int, optional
        Maximum length of formatted path string.
    fullpath : bool, optional
        Set to True to return full path rather than just the filename.
    ending_fraction : float, optional
        Fraction of string length to use for the ending of the path.
        Default: 0.5.

    Returns
    -------
    str
        Formatted (shortened) path.

    """
    if not fullpath:
        path_str = path.name
    else:
        path_str = str(path)

    if len(path_str) <= maxlen:
        return path_str

    char_end = round(maxlen * ending_fraction) - 1
    char_start = maxlen - char_end - 1

    formatted_path = path_str[:char_start] + "…" + path_str[-char_end:]
    return formatted_path


def format_item(title, author=None, year=None):
    """Format item string depending on if author and year information are
    available.

    Parameters
    ----------
    title : str
        Title of item.
    author : str
        Author information for item.
    year : str
        Publishing year for item.

    Returns
    -------
    str
        String representation of time.

    """
    if author and year:
        item_string = FORMAT_ITEM.format(title=title, author=author, year=year)
    elif author:
        item_string = FORMAT_ITEM_WITHOUT_YEAR.format(title=title, author=author)
    elif year:
        item_string = FORMAT_ITEM_WITHOUT_AUTHOR.format(title=title, year=year)
    else:
        item_string = FORMAT_ITEM_WITHOUT_AUTHOR_AND_YEAR.format(title=title)

    return item_string


def align_columns(items, sep="  ") -> list[str]:
    """Align rows of items into columns, padding with spaces as needed.

    If all values in a column are empty, that column will be excluded.

    Parameters
    ----------
    items : Iterable[tuple[str, ...]]
        An iterable over rows with tuples of strings for the different columns
    sep : str
        The separator to insert between columns.

    Returns
    -------
    list[str]
        The formatted rows.

    """
    items = list(items)
    if not items:
        return []
    # find max width of each column
    widths = [0] * len(items[0])
    for cols in items:
        for i, value in enumerate(cols):
            widths[i] = max(widths[i], len(value))

    # for each non-empty column, pad to the given width
    result = []
    for cols in items:
        row = [value.ljust(width) for value, width in zip(cols, widths) if width > 0]
        # strip trailing padding from each row
        result.append(sep.join(row).rstrip(" "))
    return result


def get_item_info(zotero_sqlite_file, query):
    """Get item information from Zotero.

    Parameters
    ----------
    zotero_sqlite_file : str
        Path to Zotero database.
    query : str
        SQL query.

    Returns
    -------
    list
        List of items return from the SQL query.

    """
    conn = sqlite3.connect(zotero_sqlite_file)
    items = list(conn.execute(query))
    conn.close()
    return items


def get_user_prefs(config_dir, profile=None):
    """Get profile preference file in ``config_dir`` directory

    Parameters
    ----------
    config_dir : str or pathlib.Path
        Path to configuration folder, from where the profiles are
        read. Usually ``~/.zotero``.

    profile : str, optional
        Name of the profile to be chosen. If None is given, then the default profile is
        chosen (the default profile is the one that is automatically selected when
        Zotero starts).
        Default: None

    Returns
    -------
    pathlib.Path or None
        Path to the preference file of the selected profile with name ``profile``.
        If ``profile`` is None, default profile is is chosen.
        Existence of the Path is not checked, will result in FileNotFoundError

    Raises
    ------
        RuntimeError: If ``profile`` is not None but no corresponding profile exists.
        RuntimeError: If no profiles are found in the directory.
    """

    config_dir = Path(config_dir) / "zotero"
    parser = configparser.ConfigParser()
    parser.read(str(config_dir / "profiles.ini"))
    config = {s: dict(parser.items(s)) for s in parser.sections()}
    profiles = {k: v for k, v in config.items() if "path" in v.keys()}
    if len(profiles) <= 0:
        raise RuntimeError(f"No profiles found in {str(config_dir)}.")

    if profile is not None:
        for k in profiles.keys():
            if "name" in profiles[k] and profiles[k]["name"] == profile:
                return config_dir / profiles[k]["path"] / "prefs.js"
        raise RuntimeError(f"No profile with name {profile}")

    # If no name is given or name not found fall back to default
    for k in profiles.keys():
        if "default" in profiles[k] and profiles[k]["default"] == "1":
            return config_dir / profiles[k]["path"] / "prefs.js"

    # If no default is found, fall back to first element
    first_key = list(profiles.keys())[0]
    return config_dir / profiles[first_key]["path"] / "prefs.js"


def get_path_pref(pref_path, preference):
    """Reads data directory from preference file

    Parameters
    ----------
    pref_path : str or pathlib.Path
        File containing user preferences. This file is assumed to exist. Raises
        FileNotFoundError otherwise.

    preference : str
        The preference setting, e.g. ``extensions.zotero.dataDir``.

    Returns
    -------
    pathlib.Path or None
        Path to the data directory or None if setting is not present.

    """
    pref_path = Path(pref_path)
    data_path = None
    for line in pref_path.read_text(encoding="utf-8").splitlines():
        if preference in line:
            # each line in pref_path is a function call with two arguments
            # of which we extract the second.
            data_path = re.search(r",.*\"(.*)\"", line)
            if data_path is not None:
                return Path(data_path[1])
            else:
                return None


def open_file(app, file_path, only_return_command=False):
    """Attempt to open ``file_path`` using ``app``.

    Parameters
    ----------
    app : str
        Application to open file. Should contain '%u' for where the file path
        would appear in the command. If '%u' is not provided, it is assumed
        that the file path is the last argument to the command.
    file_path : str
        Path to file to open.
    only_return_command : bool
        If True, only return the command and do not execute it. Default: False.

    Returns
    -------
    list
        Command that was run.

    """
    app_and_args = shlex.split(app)
    if "%u" not in app_and_args:
        app_and_args.append("%u")

    command = [arg if arg != "%u" else file_path for arg in app_and_args]

    if not only_return_command:
        subprocess.run(command)

    return [str(cmd_part) for cmd_part in command]


def show_error(error_msg, rofi_args=None):
    """Show ``error_msg`` as an error message in Rofi and stderr."""
    if rofi_args is None:
        rofi_args = []
    print(error_msg, file=sys.stderr)
    subprocess.run(["rofi", "-e", error_msg] + rofi_args)


def parse_args():
    description = "Open Zotero attachments with Rofi"
    parser = argparse.ArgumentParser(description=description)

    default_viewer_without_percent = DEFAULT_VIEWER.replace("%", "%%")

    parser.add_argument(
        "-v", "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "-p",
        "--profile",
        type=str,
        default=None,
        help=(
            "name of the Zotero profile to use. " "Default: The default Zotero profile"
        ),
    )
    parser.add_argument(
        "-l",
        "--list",
        action="store_true",
        default=False,
        help=("print out list of results instead of passing " "it to Rofi"),
    )
    parser.add_argument(
        "--rofi-args",
        type=str,
        default=DEFAULT_ROFI_ARGS,
        help=(
            f"arguments to Rofi, usually given as: "
            f'--rofi-args="-i -t theme -p title\\ with\\ space". '
            f'Default: "{DEFAULT_ROFI_ARGS}"'
        ),
    )
    parser.add_argument(
        "--viewer",
        type=str,
        default=DEFAULT_VIEWER,
        help=(
            f'application to open attachments, with "%%u" '
            f"to indicate where the path should go. "
            f'Default: "{default_viewer_without_percent}"'
        ),
    )
    parser.add_argument(
        "--prompt-paper",
        type=str,
        default=DEFAULT_PROMPT_PAPER,
        help=(
            f"prompt title when searching through papers. "
            f'Default: "{DEFAULT_PROMPT_PAPER}"'
        ),
    )
    parser.add_argument(
        "--prompt-attachment",
        type=str,
        default=DEFAULT_PROMPT_ATTACHMENT,
        help=(
            f"prompt title when searching through attachments. "
            f'Default: "{DEFAULT_PROMPT_ATTACHMENT}"'
        ),
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=DEFAULT_ZOTERO_CONFIG,
        help=(
            f"path to Zotero config directory. " f'Default: "{DEFAULT_ZOTERO_CONFIG}"'
        ),
    )
    parser.add_argument(
        "--collections",
        dest="include_collection",
        action="store_true",
        help=(
            "include the collection name along with each paper"
        ),
    )

    return parser.parse_args()


def main(
    config=None,
    profile=None,
    list=False,
    viewer="xdg-open %u",
    rofi_args="-i",
    prompt_paper="paper",
    prompt_attachment="attachment",
    include_collection=False,
):
    """Run main program.

    Parameters
    ----------
    config : str or pathlib.Path, optional
        Path to Zotero config directory. Default: DEFAULT_ZOTERO_CONFIG
    profile : str, optional
        Profile name, or None for the default profile. Default: None.
    list : bool, optional
        Set to True to print out list of results instead of passing it to Rofi.
        Default: False.
    viewer : str, optional
        Application to open attachments. Use '%u' to specify the path to the
        file to open. Default: "xdg-open %u"
    rofi_args : str, optional
        Arguments to pass to Rofi. Default: "-i"
    prompt_paper: str, optional
        Prompt title when searching through Zotero items. Default: "paper"
    prompt_attachment: str, optional
        Prompt title when searching through attachments for a specific item.
        Default: "attachment"

    """

    if config is None:
        config = DEFAULT_ZOTERO_CONFIG
    config = Path(config)

    rofi_args = shlex.split(rofi_args)

    # Check if Rofi is in PATH early on, as it is also used to show error messages
    try:
        rofi = subprocess.run(["rofi", "-v"], stdout=subprocess.DEVNULL)
    except FileNotFoundError:
        print(
            "'rofi' command not found, make sure it is installed and "
            "that it is in PATH",
            file=sys.stderr,
        )
        return

    # Compute Zotero paths
    if not config.exists():
        show_error(
            f"Zotero config directory not found: {config}\n"
            f"Use the --config option to specify the path to the config directory."
        )
        return
    try:
        pref_path = get_user_prefs(config, profile)
    except RuntimeError as e:
        show_error(
            f"{str(e)}\n" f"Use the --profile option to specify the Zotero profile."
        )
        return
    except FileNotFoundError as e:
        show_error(
            f"{str(e)}\n"
            f"Use the --config option to specify the path to the config directory."
        )
        return

    # Extract Zotero paths
    if not pref_path.exists() or not pref_path.is_file():
        show_error(
            f"Could the find preference file {str(pref_path)}.\n"
            f"Use the -c option to specify the path to the config directory.\n"
        )
        return

    zotero_path = get_path_pref(pref_path, "extensions.zotero.dataDir")
    zotero_base_dir = get_path_pref(pref_path, "extensions.zotero.baseAttachmentPath")

    if zotero_path is None:
        zotero_path = DEFAULT_ZOTERO_PATH
    if zotero_base_dir is None:
        zotero_base_dir = DEFAULT_ZOTERO_BASE_DIR

    if not zotero_path.exists():
        show_error(
            f"Zotero data directory not found: {zotero_path}\n"
            f"Use the -p option to specify the zotero profile."
        )
        return

    zotero_sqlite_file = zotero_path / _ZOTERO_SQLITE_FILE
    if not zotero_sqlite_file.exists():
        show_error(
            f"Zotero database not found: {zotero_sqlite_file}\n"
            f"Use the -p option to specify the zotero profile."
        )
        return

    # Create a temporary copy of the database to avoid issues with locking
    tmp_dir = Path(tempfile.gettempdir())
    database_copy = tmp_dir / _ZOTERO_SQLITE_FILE
    shutil.copy2(zotero_sqlite_file, database_copy)

    all_authors = get_item_info(database_copy, _QUERY_AUTHORS)
    all_titles = get_item_info(database_copy, _QUERY_TITLES)
    all_dates = get_item_info(database_copy, _QUERY_DATES)
    all_paths = get_item_info(database_copy, _QUERY_PATHS)
    if include_collection:
        all_collections = get_item_info(database_copy, _QUERY_COLLECTIONS)
        all_collection_items = get_item_info(database_copy, _QUERY_COLLECTION_ITEMS)

    os.remove(database_copy)

    author_lists = defaultdict(lambda: [])
    for author_id, author in all_authors:
        author_lists[author_id].append(author)

    authors = {
        id: format_authors(author_list) for id, author_list in author_lists.items()
    }

    titles = {}
    keys = {}
    for title_id, title, key in all_titles:
        if title_id not in titles:
            titles[title_id] = title
            keys[title_id] = key

    years = {}
    for date_id, date in all_dates:
        if date_id not in years:
            years[date_id] = date.split("-")[0]

    if include_collection:
        collection_paths = construct_collection_paths(all_collections)
        collections = {}
        for item_id, collection_id in all_collection_items:
            collections[item_id] = collection_paths[collection_id]

    paths = {}
    for path_id, path, title in all_paths:
        if path is None:
            continue

        if path_id in paths and str(paths[path_id][0]) == path:
            continue

        if path.startswith("attachments:"):
            path = path.replace("attachments:", "", 1)
            path = zotero_base_dir / path
        elif path.startswith("storage:"):
            path = path.replace("storage:", "", 1)
            path = zotero_path / _ZOTERO_STORAGE_DIR / keys[path_id] / path
        else:
            path = Path(path)

        if path_id not in paths:
            paths[path_id] = []
        paths[path_id].append((path, title))

    item_list_with_ids = []
    for item_id, title in titles.items():
        author = authors.get(item_id)
        year = years.get(item_id)
        if include_collection:
            collection = collections.get(item_id, "")
        else:
            collection = ""
        item_string = format_item(
            title=title,
            author=author,
            year=year,
        )
        order_key = (year, item_string, item_id)
        item_list_with_ids.append((order_key, item_string, item_id, collection))

    item_list_with_ids.sort()
    item_list = align_columns(
        (collection, item) for _, item, _, collection in item_list_with_ids
    )
    items_input = "\n".join(item_list)

    if list:
        print(items_input)
        return

    # NOTE: Rofi seems to prefer the first -p argument, so if -p is given in rofi_args,
    # the latter -p argument should be ignored
    rofi_command = ["rofi", "-dmenu", "-format", "i"] + rofi_args + ["-p", prompt_paper]
    rofi = subprocess.run(
        rofi_command, capture_output=True, text=True, input=items_input
    )

    selected_index = rofi.stdout.strip()
    if not selected_index:
        return

    item_id = item_list_with_ids[int(selected_index)][2]
    files_to_open = paths[item_id]
    files_to_open.sort()

    if len(files_to_open) == 1:
        file_to_open = files_to_open[0][0]
    else:
        format_opts = {
            "maxlen": FORMAT_PATH_MAXLEN,
            "fullpath": FORMAT_PATH_FULLPATH,
            "ending_fraction": FORMAT_PATH_ENDING_FRACTION,
        }
        # True if there are any files with the same name
        disamb = len(set(p for p, _ in files_to_open)) != len(files_to_open)
        files = [(title if disamb else "", format_path(p, **format_opts)) for p, title in files_to_open]
        files_input = "\n".join(align_columns(files))

        rofi_command = (
            ["rofi", "-dmenu", "-format", "i"] + rofi_args + ["-p", prompt_attachment]
        )
        rofi = subprocess.run(
            rofi_command, capture_output=True, text=True, input=files_input
        )
        selected_index = rofi.stdout.strip()
        if not selected_index:
            return

        file_to_open = files_to_open[int(selected_index)][0]

    if file_to_open.exists():
        try:
            open_file(viewer, file_to_open)
        except FileNotFoundError:
            viewer_command = open_file(viewer, file_to_open, only_return_command=True)
            viewer_command = [str(cmd_part) for cmd_part in viewer_command]
            viewer_command = " ".join(viewer_command)
            show_error(
                f"Could not run command: '{viewer_command}'\n"
                f"Make sure the viewer is installed"
            )
    else:
        show_error(f"Could not find file: {file_to_open}", rofi_args)


if __name__ == "__main__":
    args = parse_args()
    main(**vars(args))
