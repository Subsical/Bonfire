import discord

from lib import theme

TIMEOUT = 5*60
CHALLENGE_TIMEOUT = 2*60

PLAYING: set[int] = set()
def busy(user: discord.User | None) -> bool:
	return user is not None and user.id in PLAYING

RPS_CHOICES = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
RPS_BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}

MARKS = ["X", "O"]
MARK_STYLES = [discord.ButtonStyle.green, discord.ButtonStyle.red]
DISCS = ["🔴", "🟡"]
EMPTY = "⚫"

COLUMNS = 7
ROWS = 6

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
	for row in range(ROWS - 1, -1, -1):
		if board[row][column] is None:
			board[row][column] = player
			return row
	return None

def connectfour_winner(board: list[list[int | None]]) -> int | None:
	"""The player with four in a row, if there is one."""
	for row in range(ROWS):
		for col in range(COLUMNS):
			player = board[row][col]
			if player is None:
				continue
			for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
				cells = [(row + dr * i, col + dc * i) for i in range(4)]
				if all(0 <= r < ROWS and 0 <= c < COLUMNS and board[r][c] == player for r, c in cells):
					return player
	return None

def board_full(board: list[list[int | None]]) -> bool:
	return all(cell is not None for cell in board[0])

########## ======================================================================== ##########

class GameView(discord.ui.LayoutView):
	"""Shared functions: who's playing, whose turn it is, and who's allowed to press what."""

	def __init__(self, challenger: discord.User, opponent: discord.User | None):
		super().__init__(timeout=TIMEOUT)
		self.players: list[discord.User | None] = [challenger, opponent]
		self.turn = 1 if opponent is not None else 0
		self.finished = False
		self.forfeited_by: int | None = None
		self.message: discord.Message | None = None
		for player in self.players:
			if player is not None:
				PLAYING.add(player.id)

	@property
	def open_challenge(self) -> bool:
		return self.players[1] is None

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
		for player in self.players:
			if player is not None:
				PLAYING.discard(player.id)

	def forfeit_row(self) -> discord.ui.ActionRow:
		row = discord.ui.ActionRow()
		row.add_item(ForfeitButton(disabled=self.finished))
		return row

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

	async def on_timeout(self):
		self.finish()
		self.disable_all()
		if self.message is not None:
			try:
				await self.message.edit(view=self)
			except discord.HTTPException:
				pass

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
			lines = [f"{name_of(self.players[0])} {RPS_CHOICES[first]}  •  {RPS_CHOICES[second]} {name_of(self.players[1])}", ""]
			lines.append("Game over, it's a draw." if winner is None else f"Game over, **{name_of(self.players[winner])}** wins!")
			container.add_item(discord.ui.TextDisplay("\n".join(lines)))
		else:
			locked = [name_of(p) for p, pick in zip(self.players, self.picks) if pick is not None]
			waiting = "Picked: " + ", ".join(locked) if locked else "Nobody has picked yet."
			container.add_item(discord.ui.TextDisplay(waiting))

		row = discord.ui.ActionRow()
		for choice in RPS_CHOICES:
			button = RPSButton(choice)
			button.disabled = done
			row.add_item(button)
		container.add_item(row)
		container.add_item(self.forfeit_row())
		self.add_item(container)

########## ======================================================================== ##########

class TicTacToeButton(discord.ui.Button):
	def __init__(self, square: int, player: int | None, won: bool = False):
		style = discord.ButtonStyle.gray if player is None else MARK_STYLES[player]
		super().__init__(
			label="\u200e" if player is None else MARKS[player], row=square // 3,
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
		self.board: list[int | None] = [None] * 9
		self.render()

	def render(self):
		self.clear_items()
		result = tictactoe_winner(self.board)
		full = all(square is not None for square in self.board)
		if result is not None or full:
			self.finish()

		container = discord.ui.Container(accent_color=theme.COLOR_DARK if self.finished else theme.COLOR_MAIN)
		header = f"## ⭕ Tic Tac Toe\n**{MARKS[0]}** {name_of(self.players[0])} vs **{MARKS[1]}** {name_of(self.players[1])}"
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
			container.add_item(discord.ui.TextDisplay(f"**{MARKS[self.turn]}** {name_of(self.players[self.turn])}'s turn."))

		winning = result[1] if result is not None else []
		for row_index in range(3):
			row = discord.ui.ActionRow()
			for column in range(3):
				square = row_index * 3 + column
				button = TicTacToeButton(square, self.board[square], won=square in winning)
				if self.finished:
					button.disabled = True
				row.add_item(button)
			container.add_item(row)
		container.add_item(self.forfeit_row())
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
		self.board: list[list[int | None]] = [[None] * COLUMNS for _ in range(ROWS)]
		self.render()

	def render(self):
		self.clear_items()
		winner = connectfour_winner(self.board)
		full = board_full(self.board)
		if winner is not None or full:
			self.finish()

		container = discord.ui.Container(accent_color=theme.COLOR_DARK if self.finished else theme.COLOR_MAIN)
		header = f"## 🧮 Connect Four\n{DISCS[0]} {name_of(self.players[0])} vs {DISCS[1]} {name_of(self.players[1])}"
		container.add_item(discord.ui.TextDisplay(header))
		container.add_item(discord.ui.Separator())

		grid = "\n".join("".join(EMPTY if cell is None else DISCS[cell] for cell in row) for row in self.board)
		container.add_item(discord.ui.TextDisplay(f"{grid}\n1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣"))

		gave_up = self.forfeit_text()
		if gave_up is not None:
			container.add_item(discord.ui.TextDisplay(gave_up))
		elif winner is not None:
			container.add_item(discord.ui.TextDisplay(f"Game over, **{name_of(self.players[winner])}** wins!"))
		elif full:
			container.add_item(discord.ui.TextDisplay("Game over, it's a draw."))
		else:
			container.add_item(discord.ui.TextDisplay(f"{DISCS[self.turn]} {name_of(self.players[self.turn])}'s turn."))

		for start in (0, 4):
			row = discord.ui.ActionRow()
			for column in range(start, min(start + 4, COLUMNS)):
				button = ConnectFourButton(column, self.board[0][column] is not None)
				if self.finished:
					button.disabled = True
				row.add_item(button)
			container.add_item(row)
		container.add_item(self.forfeit_row())
		self.add_item(container)

########## ======================================================================== ##########

class ChallengeView(discord.ui.LayoutView):
	"""Asks the named opponent to accept before the game starts."""

	def __init__(self, challenger: discord.User, opponent: discord.User, title: str, build):
		super().__init__(timeout=CHALLENGE_TIMEOUT)
		self.challenger = challenger
		self.opponent = opponent
		self.title = title
		self.build = build  # () -> GameView
		self.settled = False
		self.message: discord.Message | None = None
		self.render()

	def render(self, footer: str | None = None):
		self.clear_items()
		container = discord.ui.Container(accent_color=theme.COLOR_DARK if self.settled else theme.COLOR_MAIN)
		container.add_item(discord.ui.TextDisplay(
			f"## {self.title}\n{self.challenger.mention} challenged {self.opponent.mention}."))
		container.add_item(discord.ui.TextDisplay(footer or "-# Waiting for them to accept..."))
		if not self.settled:
			row = discord.ui.ActionRow()
			row.add_item(AcceptButton())
			row.add_item(DeclineButton())
			container.add_item(row)
		self.add_item(container)

	async def close(self, footer: str):
		self.settled = True
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
		if interaction.user.id != challenge.opponent.id:
			await interaction.response.defer()
			return
		if busy(interaction.user) or busy(challenge.challenger):
			await interaction.response.defer()
			return
		challenge.settled = True
		game = challenge.build()
		await interaction.response.edit_message(view=game, allowed_mentions=game.ping_turn())
		game.message = await interaction.original_response()

class DeclineButton(discord.ui.Button):
	def __init__(self):
		super().__init__(label="Decline", style=discord.ButtonStyle.red)

	async def callback(self, interaction: discord.Interaction):
		challenge: ChallengeView = self.view
		if interaction.user.id not in (challenge.opponent.id, challenge.challenger.id):
			await interaction.response.defer()
			return
		who = "declined" if interaction.user.id == challenge.opponent.id else "cancelled"
		await challenge.close(f"-# The challenge was {who}.")
		await interaction.response.edit_message(view=challenge, allowed_mentions=discord.AllowedMentions.none())
