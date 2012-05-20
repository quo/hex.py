#!/usr/bin/python3

# TODO
# regex search
# go to previous match
# insert/delete bytes
# horizontal scrolling

import sys, os, mmap, curses, argparse, bisect, struct, readline

# readline sets LINES and COLUMNS, which screws up curses SIGWINCH handling
os.unsetenv('LINES')
os.unsetenv('COLUMNS')

def main():
	parser = argparse.ArgumentParser(description='View or edit a file.')
	parser.add_argument('file', metavar='FILE', help='the file to open')
	parser.add_argument('-w', '--writable', action='store_true', help='allow the file to be modified')
	args = parser.parse_args()

	with open(args.file, 'r+b' if args.writable else 'rb') as f:
		try:
			hf = MappedHexFile(f)
		except Exception as ex:
			sys.stderr.write('Failed to open file using mmap (%r), using fall-back.\n' % ex)
			hf = HexFile(f)
		with hf:
			def wrapped(scr):
				curses.use_default_colors()
				curses.mousemask(curses.ALL_MOUSE_EVENTS)
				curses.mouseinterval(0)
				curses.curs_set(2) # solid block cursor
				scr.idlok(True)
				curses.def_prog_mode()
				i = HexInterface(scr)
				i.set_file(hf)
				i.main_loop()
			curses.wrapper(wrapped)

class HexFile:
	def __init__(self, file):
		self.file = file
		file.seek(0, os.SEEK_END)
		self.size = file.tell()
	def get(self, pos, n):
		self.file.seek(pos)
		return self.file.read(n)
	def set(self, pos, buf):
		self.file.seek(pos)
		self.file.write(buf)
	def find(self, buf, start, end):
		if not buf: return start
		self.file.seek(start)
		data = b''
		while True:
			new = self.file.read(min(0x10000, end-self.file.tell()))
			if not new: return -1
			data = new if len(buf) == 1 else data[1-len(buf):] + new
			r = data.find(buf)
			if r >= 0: return self.file.tell() - len(data) + r
	def wrapfind(self, buf, start):
		r = self.find(buf, start, self.size)
		return r if r >= 0 else self.find(buf, 0, start + len(buf) - 1)
	def close(self):
		pass # we don't own self.file
	def __enter__(self):
		return self
	def __exit__(self, type, value, traceback):
		self.close()

class MappedHexFile(HexFile):
	def __init__(self, file):
		HexFile.__init__(self, file)
		self.map = mmap.mmap(file.fileno(), self.size, access=mmap.ACCESS_WRITE if file.writable() else mmap.ACCESS_READ)
	def get(self, pos, n):
		return self.map[pos:pos+n]
	def set(self, pos, buf):
		self.map[pos:pos+len(buf)] = buf
	def find(self, buf, start, end):
		return self.map.find(buf, start, end)
	def close(self):
		self.map.close()
		HexFile.close(self)

HELP_TEXT = '''
General:
  q    quit
  h    help
  :    execute a Python statement
  w    set number of columns, 0 for auto
  d    decode little endian
  D    decode big endian

Cursor movement:
  Use the mouse, arrow keys, Page Up/Down, Home & End.
  Tab  toggle hex/ascii
  g    go to relative offset, Python expressions are allowed
  G    go to absolute offset, negative means from end of file
  m    mark position
  j    jump to next mark
  J    jump to previous mark

Search:
  /    find Python expression
  \    find hex string
  n    next match

Edit:
  o    overwrite
'''.strip().splitlines()

def parse_hex(hex):
	hex = hex.replace(' ', '')
	if len(hex) == 0 or len(hex) % 2: raise ValueError()
	return bytes(int(hex[x:x+2], 16) for x in range(0, len(hex), 2))

def display_char(i):
	if i == 0: return ' '
	if 32 <= i < 127: return chr(i)
	return '\xb7' # dot

class Marks:
	def __init__(self):
		self.l = []
	def toggle(self, x):
		i = bisect.bisect_left(self.l, x)
		if i < len(self.l) and self.l[i] == x: self.l.pop(i)
		else: self.l.insert(i, x)
	def range(self, l, r):
		return self.l[bisect.bisect_left(self.l, l):bisect.bisect_left(self.l, r)]
	def next(self, x):
		return self.l[bisect.bisect_right(self.l, x) % len(self.l)] if self.l else x
	def prev(self, x):
		return self.l[bisect.bisect_left(self.l, x)-1] if self.l else x

class HexInterface:

	def __init__(self, scr):
		self.scr = scr
		self.status = 'press [h] for help'
		self.hexcursor = True
		self.needle = b''
		self.fixedcols = 0
		self.exec_globals = {'self': self}

	def set_file(self, file):
		self.file = file
		self.addrw = len('%04x' % (file.size - 1))
		self.pos = 0
		self.marks = Marks()
		self.resize()

	def main_loop(self):
		while True:
			self.draw_status()
			self.scroll_to_cursor()
			c = self.scr.getch()
			self.status = self.file.file.name
			if c == curses.KEY_RESIZE:
				self.resize()
			elif c == curses.KEY_MOUSE:
				self.process_mouse(*curses.getmouse())
			else:
				if 0 < c < 128: c = chr(c)
				if c == 'q': return
				self.process_key(c)
			self.pos = max(0, min(self.pos, self.file.size-1))
	
	def resize(self):
		self.h, self.w = self.scr.getmaxyx()
		self.h -= 1
		self.cols = self.fixedcols or max(1, (self.w - 7 - self.addrw) // 4)
		self.lines = (self.file.size-1) // self.cols + 1
		self.curline = min(max(0, self.lines - self.h), self.pos // self.cols)
		self.draw(0, self.h)
		self.draw_scrollbar()

	def read_string_getstr(self, prompt):
		self.scr.attron(curses.A_REVERSE)
		try: self.scr.addstr(self.h, 0, prompt.ljust(self.w))
		except curses.error: pass # end of screen
		curses.echo()
		s = self.scr.getstr(self.h, len(prompt))
		curses.noecho()
		self.scr.attroff(curses.A_REVERSE)
		return s.decode('ascii')

	def read_string_readline(self, prompt):
		self.scr.move(self.h, 0)
		self.scr.clrtoeol()
		self.scr.refresh()
		curses.reset_shell_mode()
		s = input(prompt)
		curses.reset_prog_mode()
		self.scr.redrawwin()
		return s

	read_string = read_string_readline

	def read_expression(self, prompt, rettype):
		r = eval(self.read_string(prompt), self.exec_globals)
		if rettype is bytes and isinstance(r, str): r = r.encode('latin1')
		if not isinstance(r, rettype): raise TypeError('expected %s, got %s' % (rettype.__name__, type(r).__name__))
		return r

	def draw(self, top, bottom):
		for y in range(top, bottom):
			data = self.file.get((self.curline+y)*self.cols, self.cols)
			s = ''
			if data:
				hexdigits = ' '.join('%02x' % d for d in data)
				asciitext = ''.join(map(display_char, data))
				s = '%0*x   %-*s  %s' % (self.addrw, (self.curline+y)*self.cols, self.cols*3, hexdigits, asciitext)
			self.scr.addstr(y, 0, s[:self.w-1] if len(s) >= self.w else s.ljust(self.w-1))
		start = (self.curline+top)*self.cols
		end = (self.curline+bottom)*self.cols
		for m in self.marks.range(start, end):
			y, x = divmod(m, self.cols)
			for x, w in ((self.addrw+3+x*3, 2), (self.addrw+3+self.cols*3+2+x, 1)):
				if x+w < self.w:
					self.scr.chgat(y-self.curline, x, w, curses.A_REVERSE)

	def draw_scrollbar(self):
		self.scr.vline(0, self.w-1, curses.ACS_VLINE, self.h)
		griph = max(1, self.h*self.h//max(1,self.lines))
		start = round(self.curline * (self.h-griph) / max(1, self.lines-self.h))
		self.scr.vline(start, self.w-1, curses.A_REVERSE, griph)
	
	def draw_status(self):
		pos = ' %x / %x' % (self.pos, self.file.size)
		l = self.w - len(pos)
		s = self.status
		try: self.scr.addstr(self.h, 0, ((s[:l-1] + '>') if len(s) > l else s.ljust(l)) + pos, curses.A_REVERSE)
		except curses.error: pass # end of screen
	
	def show_exception(self, ex):
		self.status = type(ex).__name__ + ': ' + str(ex)
	
	def scroll_to_cursor(self):
		y, x = divmod(self.pos, self.cols)
		if not 0 <= y - self.curline < self.h:
			delta = self.curline - y + (0 if y < self.curline else self.h-1)
			self.curline -= delta
			if abs(delta) < self.h:
				self.scr.move(0, 0)
				self.scr.insdelln(delta)
			if delta > 0: self.draw(0, min(delta, self.h))
			else: self.draw(max(self.h + delta, 0), self.h)
			self.draw_scrollbar()
			self.draw_status()
		x = self.addrw + 3 + (x * 3 if self.hexcursor else self.cols * 3 + 2 + x)
		if x < self.w: self.scr.move(y - self.curline, x)
	
	def pager(self, lines):
		for p in range(0, len(lines), self.h):
			self.scr.erase()
			for i, l in enumerate(lines[p:p+self.h]):
				self.scr.addstr(i, 0, l)
			self.scr.addstr(self.h, 0, 'press a key', curses.A_REVERSE)
			if self.scr.getch() in (curses.KEY_RESIZE, ord('q')): break
		self.resize()

	def process_key(self, c):
		if c == 'h': # help
			self.pager(HELP_TEXT)
		elif c == ':': # exec
			cmd = self.read_string(':')
			try:
				try:
					result = eval(cmd, self.exec_globals)
				except SyntaxError:
					result = exec(cmd, self.exec_globals)
				if result is None:
					self.status = ''
				else:
					self.exec_globals['__builtins__']['_'] = result
					self.status = repr(result)
			except Exception as ex: self.show_exception(ex)
		elif c == 'w': # width
			try: self.fixedcols = self.read_expression('set number of columns to: ', int)
			except Exception as ex: self.show_exception(ex)
			else: self.resize()
		elif c == '\t': self.hexcursor = not self.hexcursor
		elif c in ('g', 'G'): # go to offset
			try: offset = self.read_expression('go to '+('relative' if c=='g' else 'absolute')+' offset: ', int)
			except Exception as ex: self.show_exception(ex)
			else:
				if c == 'g': self.pos += offset
				elif offset >= 0: self.pos = offset
				else: self.pos = self.file.size + offset
		elif c == 'm': # mark
			self.marks.toggle(self.pos)
			line = self.pos//self.cols - self.curline
			self.draw(line, line+1)
		elif c == 'j': # jump
			self.pos = self.marks.next(self.pos)
		elif c == 'J':
			self.pos = self.marks.prev(self.pos)
		elif c in ('d', 'D'): # decode
			buf = self.file.get(self.pos, 8).ljust(8, b'\0')
			e = '<' if c == 'd' else '>'
			self.status = '{:08b} {}'.format(buf[0], ' '.join(t + ':' + repr(struct.unpack_from(e+t, buf)[0]) for t in 'bBhHlLfd'))
		elif c in ('/', '\\', 'n'): # find
			if c == '/':
				try: self.needle = self.read_expression(c, bytes)
				except Exception as ex:
					self.show_exception(ex)
					return
			elif c == '\\':
				try: self.needle = parse_hex(self.read_string(c))
				except ValueError:
					self.status = 'could not parse search string'
					return
			resultpos = self.file.wrapfind(self.needle, self.pos+1)
			if resultpos < 0 or not self.needle: self.status = 'nothing found'
			else: self.pos = resultpos
		elif c == 'o': # overwrite bytes
			if self.file.file.writable():
				try:
					b = parse_hex(self.read_string('overwrite with: '))
				except ValueError:
					self.status = 'could not parse hex string'
				else:
					self.file.set(self.pos, b)
					self.draw(self.pos//self.cols - self.curline, self.h)
			else:
				self.status = 'not writable'
		elif c == curses.KEY_LEFT:  self.pos -= 1
		elif c == curses.KEY_RIGHT: self.pos += 1
		elif c == curses.KEY_UP:    self.pos -= self.cols
		elif c == curses.KEY_DOWN:  self.pos += self.cols
		elif c == curses.KEY_PPAGE: self.pos -= self.cols * self.h
		elif c == curses.KEY_NPAGE: self.pos += self.cols * self.h
		elif c == curses.KEY_HOME:  self.pos = 0
		elif c == curses.KEY_END:   self.pos = self.file.size - 1
		else: self.status = 'unbound key: ' + ascii(c) + ' (press [h] for help)'

	def process_mouse(self, mid, mx, my, mz, mbtn):
		if mbtn & curses.BUTTON4_PRESSED: self.pos -= self.cols * (self.h // 2 + 1) # wheel up
		if mbtn & curses.BUTTON2_PRESSED: self.pos += self.cols * (self.h // 2 + 1) # wheel down
		if mbtn & curses.BUTTON1_PRESSED and 0 <= my < self.h:
			if mx == self.w-1: # scrollbar
				self.pos = (self.file.size-1)*my//(self.h-1)
				return
			mx -= self.addrw + 3
			if 0 <= mx < self.cols * 3: # hex view
				self.pos = (self.curline + my) * self.cols + mx // 3
				self.hexcursor = True
			mx -= self.cols * 3 + 2
			if 0 <= mx < self.cols: # ascii view
				self.pos = (self.curline + my) * self.cols + mx
				self.hexcursor = False

if __name__ == '__main__': main()
