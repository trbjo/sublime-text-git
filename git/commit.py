import codecs
import functools
import tempfile
import os
from subprocess import check_output, Popen

import sublime
import sublime_plugin
from . import GitTextCommand, GitWindowCommand, plugin_file, view_contents, _make_text_safeish
from .add import GitAddSelectedHunkCommand

history = []


class PromptGitCommand(GitWindowCommand):
    last_selected = 0

    def is_enabled(self):
        view = self.window.active_view()
        if view:
            scope = view.scope_name(view.sel()[0].begin()).split(' ')[0]
            if scope == 'text.git.commit':
                return True
        return False

    def run(self):
        git_actions_pretty = [
        '1: Commit, Rebase and Push',
        '2: Commit and Push',
        '3: Commit only',
        '4: Close without committing'
        ]

        self.window.show_quick_panel(
            git_actions_pretty,
            self.transform,
            selected_index=self.last_selected)

    def transform(self, i):
        if i == -1:
            return
        self.last_selected = i

        view = sublime.active_window().active_view()

        if i != 3:
            view.run_command('save')

        view.set_scratch(True)
        view.close()

        if i >= 2:
            return

        pwd = self.window.active_view().file_name().rsplit('/', 1)[0]

        if i == 0:
            self.run_command(['git', '-C', pwd, 'pull', '--rebase'], callback=self.push)
        else:
            self.push('')


    def push(self, _) -> None:
        pwd = self.window.active_view().file_name().rsplit('/', 1)[0]
        self.run_command(['git', '-C', pwd, 'push'])


class GitCommitNewCommand(sublime_plugin.WindowCommand):
    global git_dirs
    global non_git_dirs
    git_dirs = []
    non_git_dirs = []

    def anything_to_commit(self, pwd: str) -> bool:
        get_status = ['git', '-C', pwd, 'status', '--porcelain', '--untracked-files=no']
        res_list = check_output(get_status).decode('utf-8').split('\n')
        return any(not git_status.isalpha() for git_status in res_list)

    def run(self):
        pwd = self.window.active_view().file_name().rsplit('/', 1)[0]
        try:
            Popen(['git', '-C', pwd, 'commit', '-v'])
        except:
            sublime.status_message('Nothing to commit')

    def is_enabled(self):

        view = self.window.active_view()
        if view is None:
            return False

        file = view.file_name()
        if file is None:
            return False

        pwd = file.rsplit('/', 1)[0]
        if pwd.endswith('.git'):
            pwd = pwd[0:-4]

        for repo in git_dirs:
            if repo in pwd:
                if self.anything_to_commit(pwd) == False:
                    return False
                else:
                    return True
        for directory in non_git_dirs:
            if directory in pwd:
                return False

        result = self.is_git_dir(pwd)
        if result:
            git_dirs.append(result)

            if self.anything_to_commit(pwd) == False:
                return False
            else:
                return True

        else:
            non_git_dirs.append(pwd)
            return False


    def is_git_dir(self, pwd: str) -> str:
        while pwd:
            if os.path.exists(os.path.join(pwd, '.git')):
                return pwd
            parent = os.path.realpath(os.path.join(pwd, os.path.pardir))
            if parent == pwd:
                # /.. == /
                return ''
            pwd = parent



class GitQuickCommitCommand(GitTextCommand):
    def run(self, edit, target=None):
        if target is None:
            # 'target' might also be False, in which case we just don't provide an add argument
            target = self.get_file_name()
        self.get_window().show_input_panel(
            "Message", "",
            functools.partial(self.on_input, target), None, None
        )

    def on_input(self, target, message):
        if message.strip() == "":
            self.panel("No commit message provided")
            return

        if target:
            command = ['git', 'add']
            if target == '*':
                command.append('--all')
            else:
                command.extend(('--', target))
            self.run_command(command, functools.partial(self.add_done, message))
        else:
            self.add_done(message, "")

    def add_done(self, message, result):
        if result.strip():
            sublime.error_message("Error adding file:\n" + result)
            return
        self.run_command(['git', 'commit', '-m', message])


# Commit is complicated. It'd be easy if I just wanted to let it run
# on OSX, and assume that subl was in the $PATH. However... I can't do
# that. Second choice was to set $GIT_EDITOR to sublime text for the call
#  to commit, and let that Just Work. However, on Windows you can't pass
# -w to sublime, which means the editor won't wait, and so the commit will fail
# with an empty message.
# Thus this flow:
# 1. `status --porcelain --untracked-files=no` to know whether files need
#    to be committed
# 2. `status` to get a template commit message (not the exact one git uses; I
#    can't see a way to ask it to output that, which is not quite ideal)
# 3. Create a scratch buffer containing the template
# 4. When this buffer is closed, get its contents with an event handler and
#    pass execution back to the original command. (I feel that the way this
#    is done is  a total hack. Unfortunately, I cannot see a better way right
#    now.)
# 5. Strip lines beginning with # from the message, and save in a temporary
#    file
# 6. `commit -F [tempfile]`
class GitCommitCommand(GitWindowCommand):
    active_message = False
    extra_options = ""
    quit_when_nothing_staged = True

    def run(self):
        self.lines = []
        self.working_dir = self.get_working_dir()
        self.run_command(
            ['git', 'status', '--untracked-files=no', '--porcelain'],
            self.porcelain_status_done
        )

    def porcelain_status_done(self, result):
        # todo: split out these status-parsing things... asdf
        has_staged_files = False
        result_lines = result.rstrip().split('\n')
        for line in result_lines:
            if line and not line[0].isspace():
                has_staged_files = True
                break
        if not has_staged_files and self.quit_when_nothing_staged:
            self.panel("Nothing to commit")
            return
        # Okay, get the template!
        s = sublime.load_settings("Git.sublime-settings")
        if s.get("verbose_commits"):
            self.run_command(['git', 'diff', '--staged', '--no-color'], self.diff_done)
        else:
            self.run_command(['git', 'status'], self.diff_done)

    def diff_done(self, result):
        settings = sublime.load_settings("Git.sublime-settings")
        historySize = settings.get('history_size')
        rulers = settings.get('commit_rulers')

        def format(line):
            return '# ' + line.replace("\n", " ")

        if not len(self.lines):
            self.lines = ["", ""]

        self.lines.extend(map(format, history[:historySize]))
        self.lines.extend([
            "# --------------",
            "# Please enter the commit message for your changes. Everything below",
            "# this paragraph is ignored, and an empty message aborts the commit.",
            "# Just close the window to accept your message.",
            result.strip()
        ])
        template = "\n".join(self.lines)
        msg = self.window.new_file()
        msg.set_scratch(True)
        msg.set_name("COMMIT_EDITMSG")

        if rulers:
            msg.settings().set('rulers', rulers)

        self._output_to_view(msg, template, syntax=plugin_file("syntax/Git Commit Message.tmLanguage"))
        msg.sel().clear()
        msg.sel().add(sublime.Region(0, 0))
        GitCommitCommand.active_message = self

    def message_done(self, message):
        # filter out the comments (git commit doesn't do this automatically)
        settings = sublime.load_settings("Git.sublime-settings")
        historySize = settings.get('history_size')
        lines = [
            line for line in message.split("\n# --------------")[0].split("\n")
            if not line.lstrip().startswith('#')
        ]
        message = '\n'.join(lines).strip()

        if len(message) and historySize:
            history.insert(0, message)
        # write the temp file
        message_file = tempfile.NamedTemporaryFile(delete=False)
        message_file.write(_make_text_safeish(message, self.fallback_encoding, 'encode'))
        message_file.close()
        self.message_file = message_file
        # and actually commit
        with codecs.open(message_file.name, mode='r', encoding='utf-8') as fp:
            self.run_command(
                ['git', 'commit', '-F', '-', self.extra_options],
                self.commit_done, working_dir=self.working_dir, stdin=fp.read()
            )

    def commit_done(self, result, **kwargs):
        os.remove(self.message_file.name)
        self.panel(result)


class GitCommitAmendCommand(GitCommitCommand):
    extra_options = "--amend"
    quit_when_nothing_staged = False

    def diff_done(self, result):
        self.after_show = result
        self.run_command(['git', 'log', '-n', '1', '--format=format:%B'], self.amend_diff_done)

    def amend_diff_done(self, result):
        self.lines = result.split("\n")
        super(GitCommitAmendCommand, self).diff_done(self.after_show)


class GitCommitMessageListener(sublime_plugin.EventListener):
    def on_close(self, view):
        if view.name() != "COMMIT_EDITMSG":
            return
        command = GitCommitCommand.active_message
        if not command:
            return
        message = view_contents(view)
        command.message_done(message)


class GitCommitHistoryCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        self.edit = edit
        if history:
            self.view.window().show_quick_panel(history, self.panel_done, sublime.MONOSPACE_FONT)
        else:
            sublime.message_dialog("You have no commit history.\n\nCommit history is just a quick list of messages you've used in this session.")

    def panel_done(self, index):
        if index > -1:
            self.view.replace(self.edit, self.view.sel()[0], history[index] + '\n')


class GitCommitSelectedHunk(GitAddSelectedHunkCommand):
    def cull_diff(self, result):
        super(GitCommitSelectedHunk, self).cull_diff(result)
        self.get_window().run_command('git_commit')
