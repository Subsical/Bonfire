import discord

from lib import theme

TIMEOUT = 5*60
CHALLENGE_TIMEOUT = 2*60
BLANK = "\u200e"
PLAYING: set[int] = set()
def busy(user: discord.User | None) -> bool:
	return user is not None and user.id in PLAYING

# live games and challenges by message id, so a deleted message can find what to tear down
LIVE: dict[int, "GameView | ChallengeView"] = {}

def track(view: "GameView | ChallengeView", message: discord.Message | None):
	"""Points a view at its message and files it under that message's id."""
	view.message = message
	if message is not None:
		LIVE[message.id] = view

def untrack(view: "GameView | ChallengeView"):
	if view.message is not None:
		LIVE.pop(view.message.id, None)

async def abandon(message_id: int):
	"""Ends whatever game lived on a deleted message, without saying anything."""
	view = LIVE.pop(message_id, None)
	if view is None:
		return
	view.stop()  # no timeout should fire and try to edit a message that's gone
	if isinstance(view, GameView):
		view.finish()
		await view.cleanup()
	else:
		view.settled = True

RPS_CHOICES = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
RPS_BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}

TTT_MARKS = ["X", "O"]
TTT_MARK_STYLES = [discord.ButtonStyle.green, discord.ButtonStyle.red]

C4_DISCS = ["🔴", "🟡"]
C4_EMPTY = "⚫"
C4_COLUMNS = 7
C4_ROWS = 6

BATTLESHIP = "<:battleship:1548509944096231455>"
BS_GRID = 5
BS_SHIPS = [4, 3, 2]
SHIP_EMOJI = ["🚢", "⛵", "🛶"]
HIT, MISS, SUNK = "💥", "🌀", "<:darkX:1548515647552491570>"
GAP = "<:blank:1548524695048028280>"  # spacer between the two end boards

########## ======================================================================== ##########

def name_of(user: discord.User | None) -> str:
	return user.mention if user is not None else "whoever joins"

def rps_winner(first: str, second: str) -> int | None:
	"""0 or 1 for the winning player, None for a draw."""
	if first == second:
		return None
	return 0 if RPS_BEATS[first] == second else 1

def tictactoe_winner(board: list[str | None]) -> tuple[int, list[int]] | None:
	"""(player, winning line) if someone got three in a row."""
	lines = [(0,1,2), (3,4,5), (6,7,8), (0,3,6), (1,4,7), (2,5,8), (0,4,8), (2,4,6)]
	for line in lines:
		a, b, c = line
		if board[a] is not None and board[a] == board[b] == board[c]:
			return board[a], list(line)
	return None

def connectfour_drop(board: list[list[int | None]], column: int, player: int) -> int | None:
	"""Drops a disc down a column and returns the row it landed in, or None if full."""
	for row in range(C4_ROWS - 1, -1, -1):
		if board[row][column] is None:
			board[row][column] = player
			return row
	return None

def connectfour_winner(board: list[list[int | None]]) -> int | None:
	"""The player with four in a row, if there is one."""
	for row in range(C4_ROWS):
		for col in range(C4_COLUMNS):
			player = board[row][col]
			if player is None:
				continue
			for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
				cells = [(row + dr*i, col + dc*i) for i in range(4)]
				if all(0 <= r < C4_ROWS and 0 <= c < C4_COLUMNS and board[r][c] == player for r, c in cells):
					return player
	return None

def board_full(board: list[list[int | None]]) -> bool:
	return all(cell is not None for cell in board[0])



def run_between(start: int, end: int, taken: set[int]) -> list[int] | None:
	"""The straight, clear line of squares from start to end, or None if there isn't one."""
	srow, scol = divmod(start, BS_GRID)
	erow, ecol = divmod(end, BS_GRID)
	if srow != erow and scol != ecol:
		return None  # diagonal
	step = 1 if srow == erow else BS_GRID
	low, high = min(start, end), max(start, end)
	cells = list(range(low, high + 1, step))
	return None if any(c in taken for c in cells) else cells

def grow_run(pending: list[int], square: int, size: int, taken: set[int]) -> list[int] | None:
	"""Extends a part-placed ship out to `square`, capped at `size`. None if that isn't a legal line."""
	if not pending:
		return None if square in taken else [square]
	if len(pending) == 1:
		run = run_between(pending[0], square, taken)
	else:
		# already has an axis, so the square has to carry on along it
		step = pending[1] - pending[0]
		if step == 1 and square // BS_GRID != pending[0] // BS_GRID:
			return None
		if step == BS_GRID and square % BS_GRID != pending[0] % BS_GRID:
			return None
		run = run_between(min(pending[0], square), max(pending[-1], square), taken)
	if run is None or len(run) > size:
		return None
	return run

def can_complete(run: list[int], size: int, taken: set[int]) -> bool:
	"""Whether a part-placed ship still has room to reach its full length."""
	missing = size - len(run)
	if missing == 0:
		return True
	if len(run) == 1:
		# no axis yet, so each one gets measured in both directions
		return any(len(open_reach(run[0], -step, size, taken)) + len(open_reach(run[0], step, size, taken)) - 1 >= size
			for step in (1, BS_GRID))
	step = run[1] - run[0]
	before = open_reach(run[0], -step, missing + 1, taken)
	after = open_reach(run[-1], step, missing + 1, taken)
	return len(before) - 1 + len(after) - 1 >= missing

def open_reach(square: int, step: int, limit: int, taken: set[int]) -> list[int]:
	"""How far a line can run from a square before hitting an edge, a ship or `limit` squares."""
	cells = [square]
	current = square
	while len(cells) < limit:
		_, col = divmod(current, BS_GRID)
		if abs(step) == 1 and not (0 <= col + (1 if step > 0 else -1) < BS_GRID):
			break
		nxt = current + step
		if not (0 <= nxt < BS_GRID*BS_GRID) or nxt in taken:
			break
		cells.append(nxt)
		current = nxt
	return cells

def placeable_squares(pending: list[int], size: int, taken: set[int]) -> dict[int, list[int]]:
	"""Every square worth offering next, mapped to the ship it would make."""
	found = {}
	for square in range(BS_GRID * BS_GRID):
		if square in taken or square in pending:
			continue
		run = grow_run(pending, square, size, taken)
		if run is not None and can_complete(run, size, taken):
			found[square] = run
	return found

def all_sunk(fleet: list[list[int]], shots: set[int]) -> bool:
	return all(all(cell in shots for cell in ship) for ship in fleet)

def sunk_ships(fleet: list[list[int]], shots: set[int]) -> list[list[int]]:
	return [ship for ship in fleet if all(cell in shots for cell in ship)]

def shot_result(fleet: list[list[int]], shots: set[int], square: int) -> str:
	"""What a shot at this square did, given it has already been added to shots."""
	ship = next((s for s in fleet if square in s), None)
	if ship is None:
		return "miss"
	return "sunk" if all(cell in shots for cell in ship) else "hit"

########## ======================================================================== ##########

class GameView(discord.ui.LayoutView):
	"""Shared functions: who's playing, whose turn it is, and who's allowed to press what."""

	def __init__(self, challenger: discord.User, opponent: discord.User | None):
		super().__init__(timeout=TIMEOUT)
		self.players: list[discord.User | None] = [challenger, opponent]
		self.turn = 1 if opponent is not None else 0
		self.finished = False
		self.forfeited_by: int | None = None
		self.expired = False
		self.message: discord.Message | None = None
		for player in self.players:
			if player is not None:
				PLAYING.add(player.id)

	@property
	def open_challenge(self) -> bool:
		return self.players[1] is None

	@property
	def won(self) -> bool:
		"""Ended by someone actually winning, rather than giving up or timing out."""
		return self.finished and self.forfeited_by is None and not self.expired

	def seat_of(self, user: discord.User) -> int | None:
		return next((i for i, p in enumerate(self.players) if p is not None and p.id == user.id), None)

	async def claim_seat(self, interaction: discord.Interaction) -> bool:
		"""Lets anyone take the empty seat of an open challenge. False if they can't play."""
		seat = self.seat_of(interaction.user)
		if seat is not None:
			return True
		if self.open_challenge and interaction.user.id != self.players[0].id and not busy(interaction.user):
			self.players[1] = interaction.user
			PLAYING.add(interaction.user.id)
			return True
		await interaction.response.defer()
		return False

	async def check_turn(self, interaction: discord.Interaction) -> bool:
		"""Checks whether the user is allowed to make a move or not."""
		if self.finished:
			await interaction.response.defer()
			return False
		if not await self.claim_seat(interaction):
			return False
		if self.seat_of(interaction.user) != self.turn:
			await interaction.response.defer()
			return False
		return True

	def ping_turn(self) -> discord.AllowedMentions:
		"""Lets the edit ping whoever's turn it is, and nobody else."""
		current = None if self.finished else self.players[self.turn]
		return discord.AllowedMentions(everyone=False, roles=False, users=[current] if current is not None else False)

	def finish(self):
		"""Marks the game over."""
		self.finished = True
		untrack(self)
		for player in self.players:
			if player is not None:
				PLAYING.discard(player.id)

	def forfeit_row(self) -> discord.ui.ActionRow | None:
		"""None once someone has forfeited, since there's nothing left to give up on."""
		if self.forfeited_by is not None:
			return None
		row = discord.ui.ActionRow()
		row.add_item(ForfeitButton(disabled=self.finished))
		return row

	def add_forfeit_row(self, container: discord.ui.Container):
		row = self.forfeit_row()
		if row is not None:
			container.add_item(row)

	def ending_text(self) -> str | None:
		"""The line that replaces the usual status once a game is over."""
		if self.expired:
			return "Game expired due to inactivity."
		return self.forfeit_text()

	def forfeit_text(self) -> str | None:
		"""The result line when someone gave up, or None if nobody did."""
		if self.forfeited_by is None:
			return None
		quitter, _ = self.players[self.forfeited_by], self.players[1 - self.forfeited_by]
		return f"**{name_of(quitter)}** has forfeited the game."

	def disable_all(self):
		for item in self.walk_children():
			if isinstance(item, discord.ui.Button):
				item.disabled = True

	async def cleanup(self):
		"""Anything a game needs to tear down once it stops early."""
		return

	async def on_timeout(self):
		self.expired = True
		self.finish()
		self.render()
		self.disable_all()
		if self.message is not None:
			try:
				await self.message.edit(view=self)
			except discord.HTTPException:
				pass
		await self.cleanup()

	async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
		import traceback
		traceback.print_exception(type(error), error, error.__traceback__)
		if not interaction.response.is_done():
			await interaction.response.send_message(f"{theme.ERR} Something went wrong, try again.", ephemeral=True)

class ForfeitButton(discord.ui.Button):
	def __init__(self, disabled: bool = False):
		super().__init__(label="Forfeit", style=discord.ButtonStyle.blurple, disabled=disabled)

	async def callback(self, interaction: discord.Interaction):
		game: GameView = self.view
		if game.finished:
			await interaction.response.defer()
			return
		seat = game.seat_of(interaction.user)
		if seat is None:
			await interaction.response.defer()
			return
		# an open challenge nobody joined has no winner to hand it to, so it just stops
		game.forfeited_by = seat if not game.open_challenge else None
		game.finish()
		game.render()
		game.disable_all()
		await interaction.response.edit_message(view=game, allowed_mentions=discord.AllowedMentions.none())
		await game.cleanup()

########## ======================================================================== ##########

class RPSButton(discord.ui.Button):
	def __init__(self, choice: str):
		super().__init__(label=choice.capitalize(), emoji=RPS_CHOICES[choice], style=discord.ButtonStyle.gray)
		self.choice = choice

	async def callback(self, interaction: discord.Interaction):
		game: RPSView = self.view
		if game.finished:
			await interaction.response.defer()
			return
		if not await game.claim_seat(interaction):
			return

		seat = game.seat_of(interaction.user)
		if game.picks[seat] is not None:
			await interaction.response.send_message(f"{theme.ERR} You've already chosen a hand!", ephemeral=True)
			return

		game.picks[seat] = self.choice
		await interaction.response.send_message(f"{theme.SUC} You picked {RPS_CHOICES[self.choice]} **{self.choice}**.", ephemeral=True)
		game.render()
		if game.message is not None:
			await game.message.edit(view=game, allowed_mentions=discord.AllowedMentions.none())

class RPSView(GameView):
	"""Both players pick a hand, then it reveals the winner."""

	def __init__(self, challenger: discord.User, opponent: discord.User | None):
		super().__init__(challenger, opponent)
		self.picks: list[str | None] = [None, None]
		self.render()

	def render(self):
		self.clear_items()
		done = all(p is not None for p in self.picks)
		if done:
			self.finish()

		container = discord.ui.Container(accent_color=theme.COLOR_DARK if done or self.finished else theme.COLOR_MAIN)
		container.add_item(discord.ui.TextDisplay(f"## ✊ Rock Paper Scissors\n{name_of(self.players[0])} vs {name_of(self.players[1])}"))
		container.add_item(discord.ui.Separator())

		gave_up = self.forfeit_text()
		if gave_up is not None:
			container.add_item(discord.ui.TextDisplay(gave_up))
		elif done:
			first, second = self.picks
			winner = rps_winner(first, second)
			lines = [f"{name_of(self.players[0])} {RPS_CHOICES[first]}  ×  {RPS_CHOICES[second]} {name_of(self.players[1])}", ""]
			lines.append("Game over, it's a draw." if winner is None else f"Game over, **{name_of(self.players[winner])}** wins!")
			container.add_item(discord.ui.TextDisplay("\n".join(lines)))
		elif self.expired:
			container.add_item(discord.ui.TextDisplay("Game expired due to inactivity."))
			waiting = None
		else:
			locked = [name_of(p) for p, pick in zip(self.players, self.picks) if pick is not None]
			waiting = "Picked: " + ", ".join(locked) if locked else "Nobody has picked yet."
			container.add_item(discord.ui.TextDisplay(waiting))

		if not self.won:
			row = discord.ui.ActionRow()
			for choice in RPS_CHOICES:
				button = RPSButton(choice)
				button.disabled = done or self.finished
				row.add_item(button)
			container.add_item(row)
			self.add_forfeit_row(container)
		self.add_item(container)

########## ======================================================================== ##########

class TicTacToeButton(discord.ui.Button):
	def __init__(self, square: int, player: int | None, won: bool = False):
		style = discord.ButtonStyle.gray if player is None else TTT_MARK_STYLES[player]
		super().__init__(
			label=BLANK if player is None else TTT_MARKS[player], row=square // 3,
			style=discord.ButtonStyle.blurple if won else style,
			disabled=player is not None)
		self.square = square

	async def callback(self, interaction: discord.Interaction):
		game: TicTacToeView = self.view
		if not await game.check_turn(interaction):
			return
		game.board[self.square] = game.turn
		game.turn = 1 - game.turn
		game.render()
		await interaction.response.edit_message(view=game, allowed_mentions=game.ping_turn())

class TicTacToeView(GameView):
	def __init__(self, challenger: discord.User, opponent: discord.User | None):
		super().__init__(challenger, opponent)
		self.board: list[int | None] = [None]*9
		self.render()

	def render(self):
		self.clear_items()
		result = tictactoe_winner(self.board)
		full = all(square is not None for square in self.board)
		if result is not None or full:
			self.finish()

		container = discord.ui.Container(accent_color=theme.COLOR_DARK if self.finished else theme.COLOR_MAIN)
		header = f"## ⭕ Tic Tac Toe\n**{TTT_MARKS[0]}** {name_of(self.players[0])} vs **{TTT_MARKS[1]}** {name_of(self.players[1])}"
		container.add_item(discord.ui.TextDisplay(header))
		container.add_item(discord.ui.Separator())

		gave_up = self.forfeit_text()
		if gave_up is not None:
			container.add_item(discord.ui.TextDisplay(gave_up))
		elif result is not None:
			container.add_item(discord.ui.TextDisplay(f"Game over, **{name_of(self.players[result[0]])}** wins!"))
		elif full:
			container.add_item(discord.ui.TextDisplay("Game over, it's a draw."))
		else:
			container.add_item(discord.ui.TextDisplay("Game expired due to inactivity." if self.expired
				else f"**{TTT_MARKS[self.turn]}** {name_of(self.players[self.turn])}'s turn."))

		winning = result[1] if result is not None else []
		for row_index in range(3):
			row = discord.ui.ActionRow()
			for column in range(3):
				square = row_index*3 + column
				button = TicTacToeButton(square, self.board[square], won=square in winning)
				if self.finished:
					button.disabled = True
				row.add_item(button)
			container.add_item(row)
		if not self.finished:
			self.add_forfeit_row(container)
		self.add_item(container)

########## ======================================================================== ##########

class ConnectFourButton(discord.ui.Button):
	def __init__(self, column: int, full: bool):
		super().__init__(label=str(column + 1), style=discord.ButtonStyle.gray, disabled=full, row=column // 4)
		self.column = column

	async def callback(self, interaction: discord.Interaction):
		game: ConnectFourView = self.view
		if not await game.check_turn(interaction):
			return
		# a full column's button is already disabled by render(), so this can't normally fail
		if connectfour_drop(game.board, self.column, game.turn) is None:
			await interaction.response.defer()
			return
		game.turn = 1 - game.turn
		game.render()
		await interaction.response.edit_message(view=game, allowed_mentions=game.ping_turn())

class ConnectFourView(GameView):
	def __init__(self, challenger: discord.User, opponent: discord.User | None):
		super().__init__(challenger, opponent)
		self.board: list[list[int | None]] = [[None]*C4_COLUMNS for _ in range(C4_ROWS)]
		self.render()

	def render(self):
		self.clear_items()
		winner = connectfour_winner(self.board)
		full = board_full(self.board)
		if winner is not None or full:
			self.finish()

		container = discord.ui.Container(accent_color=theme.COLOR_DARK if self.finished else theme.COLOR_MAIN)
		header = f"## 🧮 Connect Four\n{C4_DISCS[0]} {name_of(self.players[0])} vs {C4_DISCS[1]} {name_of(self.players[1])}"
		container.add_item(discord.ui.TextDisplay(header))
		container.add_item(discord.ui.Separator())

		grid = "\n".join("".join(C4_EMPTY if cell is None else C4_DISCS[cell] for cell in row) for row in self.board)
		container.add_item(discord.ui.TextDisplay(f"{grid}\n1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣"))

		gave_up = self.forfeit_text()
		if gave_up is not None:
			container.add_item(discord.ui.TextDisplay(gave_up))
		elif winner is not None:
			container.add_item(discord.ui.TextDisplay(f"Game over, **{name_of(self.players[winner])}** wins!"))
		elif full:
			container.add_item(discord.ui.TextDisplay("Game over, it's a draw."))
		else:
			container.add_item(discord.ui.TextDisplay("Game expired due to inactivity." if self.expired
				else f"{C4_DISCS[self.turn]} {name_of(self.players[self.turn])}'s turn."))

		if not self.won:
			for start in (0, 4):
				row = discord.ui.ActionRow()
				for column in range(start, min(start + 4, C4_COLUMNS)):
					button = ConnectFourButton(column, self.board[0][column] is not None)
					if self.finished:
						button.disabled = True
					row.add_item(button)
				container.add_item(row)
			self.add_forfeit_row(container)
		self.add_item(container)

########## ======================================================================== ##########

class ChallengeView(discord.ui.LayoutView):
	"""Asks someone to accept before the game starts. With no opponent it's an open invite."""

	def __init__(self, challenger: discord.User, opponent: discord.User | None, title: str, build):
		super().__init__(timeout=CHALLENGE_TIMEOUT)
		self.challenger = challenger
		self.opponent = opponent
		self.title = title
		self.build = build  # (opponent) -> GameView
		self.settled = False
		self.message: discord.Message | None = None
		self.render()

	@property
	def open_invite(self) -> bool:
		return self.opponent is None

	def render(self, footer: str | None = None):
		self.clear_items()
		container = discord.ui.Container(accent_color=theme.COLOR_DARK if self.settled else theme.COLOR_MAIN)
		if self.open_invite:
			line = f"{self.challenger.mention} is looking for someone to play."
			waiting = "-# Anyone can accept."
		else:
			line = f"{self.challenger.mention} challenged {self.opponent.mention}."
			waiting = "-# Waiting for them to accept..."
		container.add_item(discord.ui.TextDisplay(f"## {self.title}\n{line}"))
		container.add_item(discord.ui.TextDisplay(footer or waiting))
		if not self.settled:
			row = discord.ui.ActionRow()
			row.add_item(AcceptButton())
			row.add_item(CancelButton())
			container.add_item(row)
		self.add_item(container)

	async def close(self, footer: str):
		self.settled = True
		untrack(self)
		self.render(footer)

	async def on_timeout(self):
		if self.settled:
			return
		await self.close("-# The challenge timed out.")
		if self.message is not None:
			try:
				await self.message.edit(view=self)
			except discord.HTTPException:
				pass

class AcceptButton(discord.ui.Button):
	def __init__(self):
		super().__init__(label="Accept", style=discord.ButtonStyle.green)

	async def callback(self, interaction: discord.Interaction):
		challenge: ChallengeView = self.view
		if challenge.open_invite:
			if interaction.user.id == challenge.challenger.id:
				await interaction.response.defer()
				return
		elif interaction.user.id != challenge.opponent.id:
			await interaction.response.defer()
			return
		if busy(interaction.user) or busy(challenge.challenger):
			await interaction.response.defer()
			return
		challenge.settled = True
		untrack(challenge)
		game = challenge.build(interaction.user)
		await interaction.response.edit_message(view=game, allowed_mentions=game.ping_turn())
		track(game, await interaction.original_response())

class CancelButton(discord.ui.Button):
	def __init__(self):
		super().__init__(label="Cancel", style=discord.ButtonStyle.gray)

	async def callback(self, interaction: discord.Interaction):
		challenge: ChallengeView = self.view
		allowed = {challenge.challenger.id}
		if not challenge.open_invite:
			allowed.add(challenge.opponent.id)
		if interaction.user.id not in allowed:
			await interaction.response.defer()
			return
		await challenge.close("-# The challenge was cancelled.")
		await interaction.response.edit_message(view=challenge, allowed_mentions=discord.AllowedMentions.none())

########## ======================================================================== ##########

class PlacementCell(discord.ui.Button):
	"""One square on a player's private setup board."""

	def __init__(self, square: int, label: str | None, emoji: str | None, style: discord.ButtonStyle, disabled: bool):
		super().__init__(label=label, emoji=emoji, style=style, disabled=disabled, row=square // BS_GRID)
		self.square = square

	async def callback(self, interaction: discord.Interaction):
		setup: PlacementView = self.view
		setup.opened_by = interaction
		if self.square in setup.pending:
			setup.pending = []  # tapping the ship again takes it back
		else:
			run = grow_run(setup.pending, self.square, setup.size, setup.taken)
			if run is None:
				setup.pending = [self.square]  # not on the line, so start the ship here
			elif len(run) == setup.size:
				setup.fleet.append(run)
				setup.taken.update(run)
				setup.pending = []
				if setup.done:
					await interaction.response.edit_message(view=setup.rebuild())
					await setup.game.player_ready(interaction, setup)
					return
			else:
				setup.pending = run
		await interaction.response.edit_message(view=setup.rebuild())

class ResetButton(discord.ui.Button):
	def __init__(self):
		super().__init__(label="Start over", style=discord.ButtonStyle.gray)

	async def callback(self, interaction: discord.Interaction):
		setup: PlacementView = self.view
		setup.fleet.clear()
		setup.taken.clear()
		setup.pending = []
		await interaction.response.edit_message(view=setup.rebuild())

class PlacementView(discord.ui.LayoutView):
	"""A player's private board for laying out their fleet."""

	def __init__(self, game: "BattleshipView", seat: int):
		super().__init__(timeout=TIMEOUT)
		self.game = game
		self.seat = seat
		self.fleet: list[list[int]] = []
		self.taken: set[int] = set()
		self.pending: list[int] = []
		self.opened_by: discord.Interaction | None = None
		self.rebuild()

	@property
	def done(self) -> bool:
		return len(self.fleet) == len(BS_SHIPS)

	@property
	def size(self) -> int:
		return BS_SHIPS[len(self.fleet)] if not self.done else 0

	async def dismiss(self):
		"""Closes this player's ephemeral board, if the interaction that opened it still lives."""
		interaction = self.opened_by
		if interaction is None or interaction.is_expired():
			return
		try:
			await interaction.delete_original_response()
		except discord.HTTPException:
			pass

	def rebuild(self, status: bool = True):
		"""`status` is off when the board is just being reopened, where the waiting line is noise."""
		self.clear_items()
		container = discord.ui.Container(accent_color=theme.COLOR_MAIN)

		if self.done:
			line = (f"\n{theme.LOADING} Waiting for your opponent..." if self.game.placing else "\nBoth fleets are set!") if status else ""
			container.add_item(discord.ui.TextDisplay(f"## {BATTLESHIP} Your fleet{line}"))
			hits = self.game.shots[self.seat]
			for row in range(BS_GRID):
				action_row = discord.ui.ActionRow()
				for col in range(BS_GRID):
					square = row*BS_GRID + col
					ship_index = next((i for i, ship in enumerate(self.fleet) if square in ship), None)
					if ship_index is not None:
						emoji = HIT if square in hits else SHIP_EMOJI[ship_index]
						label, style = None, discord.ButtonStyle.red if square in hits else discord.ButtonStyle.green
					elif square in hits:
						label, emoji, style = None, MISS, discord.ButtonStyle.blurple
					else:
						label, emoji, style = BLANK, None, discord.ButtonStyle.blurple
					action_row.add_item(PlacementCell(square, label, emoji, style, disabled=True))
				container.add_item(action_row)
			self.add_item(container)
			return self

		spots = placeable_squares(self.pending, self.size, self.taken)
		if not self.pending:
			ask = f"Pick where your **{self.size}**-tile ship starts."
		else:
			left = self.size - len(self.pending)
			ask = f"**{left}** more tile{'' if left == 1 else 's'} to go, or tap the ship to start over."
		container.add_item(discord.ui.TextDisplay(
			f"## {BATTLESHIP} Place your fleet\nShip {len(self.fleet) + 1} of {len(BS_SHIPS)}\n{ask}"))

		for row in range(BS_GRID):
			action_row = discord.ui.ActionRow()
			for col in range(BS_GRID):
				square = row*BS_GRID + col
				ship_index = next((i for i, ship in enumerate(self.fleet) if square in ship), None)
				if ship_index is not None:
					label, emoji, style = None, SHIP_EMOJI[ship_index], discord.ButtonStyle.green
					usable = False
				elif square in self.pending:
					label, emoji, style = None, SHIP_EMOJI[len(self.fleet)], discord.ButtonStyle.blurple
					usable = True  # tapping it again cancels
				else:
					label, emoji = BLANK, None
					usable = square in spots
					style = discord.ButtonStyle.green if usable and self.pending else discord.ButtonStyle.blurple
				action_row.add_item(PlacementCell(square, label, emoji, style, disabled=not usable))
			container.add_item(action_row)

		if self.fleet or self.pending:
			controls = discord.ui.ActionRow()
			controls.add_item(ResetButton())
			container.add_item(controls)

		self.add_item(container)
		return self

########## ======================================================================== ##########

class ShowBoardButton(discord.ui.Button):
	"""Opens a player's private setup board."""

	def __init__(self, disabled: bool=False):
		super().__init__(label="Show board", emoji=SHIP_EMOJI[0], style=discord.ButtonStyle.gray, disabled=disabled)

	async def callback(self, interaction: discord.Interaction):
		game: BattleshipView = self.view
		seat = game.seat_of(interaction.user)
		if seat is None and not await game.claim_seat(interaction):
			return
		seat = game.seat_of(interaction.user)
		setup = game.setups.get(seat)
		if setup is None:
			if not game.placing:
				await interaction.response.defer()
				return
			setup = PlacementView(game, seat)
			game.setups[seat] = setup
		setup.opened_by = interaction
		await interaction.response.send_message(view=setup.rebuild(status=False), ephemeral=True)

class ShotCell(discord.ui.Button):
	"""One square of the opponent's waters."""

	def __init__(self, square: int, label: str | None, emoji: str | None, style: discord.ButtonStyle, disabled: bool):
		super().__init__(label=label, emoji=emoji, style=style, disabled=disabled, row=square // BS_GRID)
		self.square = square

	async def callback(self, interaction: discord.Interaction):
		game: BattleshipView = self.view
		if not await game.check_turn(interaction):
			return
		target = 1 - game.turn
		if self.square in game.shots[target]:
			await interaction.response.defer()
			return

		game.shots[target].add(self.square)
		outcome = shot_result(game.setups[target].fleet, game.shots[target], self.square)
		game.last = outcome
		if all_sunk(game.setups[target].fleet, game.shots[target]):
			game.winner = game.turn
			game.finish()
		elif outcome == "miss":
			game.turn = target
		game.render()
		await interaction.response.edit_message(view=game, allowed_mentions=game.ping_turn())

class BattleshipView(GameView):
	"""Both players lay out a fleet in private, then take turns shooting."""

	def __init__(self, challenger: discord.User, opponent: discord.User | None):
		super().__init__(challenger, opponent)
		self.setups: dict[int, PlacementView] = {}
		self.shots: list[set[int]] = [set(), set()]
		self.winner: int | None = None
		self.last: str | None = None
		self.render()

	@property
	def placing(self) -> bool:
		return not all(self.setups.get(seat) is not None and self.setups[seat].done for seat in (0, 1))

	async def player_ready(self, interaction: discord.Interaction, setup: PlacementView):
		"""Called once a player has placed their last ship."""
		if not self.placing:
			# both fleets are set, so the setup boards have served their purpose
			for other in self.setups.values():
				await other.dismiss()
		self.render()
		if self.message is not None:
			try:
				await self.message.edit(view=self, allowed_mentions=self.ping_turn())
			except discord.HTTPException:
				pass

	async def cleanup(self):
		"""A game cut short leaves the setup boards open, so they get closed here."""
		for setup in self.setups.values():
			await setup.dismiss()

	def ping_turn(self) -> discord.AllowedMentions:
		if self.placing or self.finished:
			return discord.AllowedMentions.none()
		return super().ping_turn()

	def board_rows(self, seat: int) -> list[str]:
		"""One player's waters as five rows, ships and all."""
		setup = self.setups[seat]
		shots = self.shots[seat]
		rows = []
		for row in range(BS_GRID):
			cells = []
			for col in range(BS_GRID):
				square = row*BS_GRID + col
				ship_index = next((i for i, ship in enumerate(setup.fleet) if square in ship), None)
				if ship_index is not None:
					cells.append(HIT if square in shots else SHIP_EMOJI[ship_index])
				else:
					cells.append(MISS if square in shots else "🟦")
			rows.append("".join(cells))
		return rows

	def recap(self) -> str:
		"""Both fleets revealed side by side, left one is player 1."""
		seats = [seat for seat in (0, 1) if self.setups.get(seat) is not None]
		if not seats:
			return ""
		boards = [self.board_rows(seat) for seat in seats]
		return "\n".join(GAP.join(board[row] for board in boards) for row in range(BS_GRID))

	def render(self):
		self.clear_items()
		container = discord.ui.Container(accent_color=theme.COLOR_DARK if self.finished else theme.COLOR_MAIN)
		container.add_item(discord.ui.TextDisplay(
			f"## {BATTLESHIP} Battleship\n{name_of(self.players[0])} vs {name_of(self.players[1])}"))
		container.add_item(discord.ui.Separator())

		gave_up = self.forfeit_text()
		if self.winner is not None:
			container.add_item(discord.ui.TextDisplay(
				f"**{name_of(self.players[self.winner])}** sank the fleet and wins."))
			if not self.placing:
				container.add_item(discord.ui.Separator())
				container.add_item(discord.ui.TextDisplay(self.recap()))
		elif (gave_up is not None or self.expired) and self.placing:
			container.add_item(discord.ui.TextDisplay(
				gave_up if gave_up is not None else "Game expired due to inactivity."))
		elif self.placing:
			waiting = []
			for seat in (0, 1):
				player = self.players[seat]
				if player is None:
					continue
				setup = self.setups.get(seat)
				mark = "✅" if setup is not None and setup.done else theme.LOADING
				waiting.append(f"{mark} {name_of(player)}")
			container.add_item(discord.ui.TextDisplay(
				"Both players need to place their fleet.\n" + "  •  ".join(waiting)))
		else:
			note = {"hit": "A hit!", "sunk": "Ship sunk!", "miss": "Missed."}.get(self.last, "")
			container.add_item(discord.ui.TextDisplay(
				gave_up if gave_up is not None
				else "Game expired due to inactivity." if self.expired
				else f"{note} {name_of(self.players[self.turn])} is firing at {name_of(self.players[1 - self.turn])}.".strip()))
			target = 1 - self.turn
			fleet = self.setups[target].fleet
			sunk = {cell for ship in sunk_ships(fleet, self.shots[target]) for cell in ship}
			for row_index in range(BS_GRID):
				action_row = discord.ui.ActionRow()
				for col in range(BS_GRID):
					square = row_index*BS_GRID + col
					fired = square in self.shots[target]
					if square in sunk:
						label, emoji, style = None, SUNK, discord.ButtonStyle.gray
					elif fired and any(square in ship for ship in fleet):
						label, emoji, style = None, HIT, discord.ButtonStyle.red
					elif fired:
						label, emoji, style = None, MISS, discord.ButtonStyle.blurple
					else:
						label, emoji, style = BLANK, None, discord.ButtonStyle.blurple
					action_row.add_item(ShotCell(square, label, emoji, style,
						disabled=fired or self.finished))
				container.add_item(action_row)

		if not self.won:
			controls = discord.ui.ActionRow()
			controls.add_item(ShowBoardButton(disabled=self.finished))
			container.add_item(controls)
			self.add_forfeit_row(container)
		self.add_item(container)
