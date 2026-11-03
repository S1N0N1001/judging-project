import streamlit as st
import qrcode
from io import BytesIO
import socket
import json
import string
import random
import re
import threading
import os
from datetime import datetime

DB_FILE = 'db.json'
RESULTS_FILE = 'results.txt'
db_lock = threading.Lock()


# ==========================================================
#                   УТИЛИТЫ / БАЗА ДАННЫХ
# ==========================================================

def generate_code():
	alphabet = string.ascii_letters + string.digits
	return ''.join(random.choices(alphabet, k=30))


def default_db():
	return {
		'secretary_code': generate_code(),
		'judge_codes': {},
		'judges_count': 0,
		'commands_num': 0,
		'commands': {},
	}


def _read_db_unlocked():
	if not os.path.exists(DB_FILE):
		db = default_db()
		with open(DB_FILE, 'w', encoding='utf-8') as f:
			json.dump(db, f, ensure_ascii=False, indent=4)
		return db

	with open(DB_FILE, 'r', encoding='utf-8') as f:
		db = json.load(f)

	# Миграция из старого формата
	if 'commands' not in db:
		old = db
		new_db = default_db()
		new_db['commands_num'] = old.get('commands_num', 0)
		for k, v in old.items():
			if k == 'commands_num':
				continue
			if isinstance(v, dict) and 'title' in v:
				new_db['commands'][k] = {
					'title': v.get('title', ''),
					'description': v.get('description', ''),
					'members': v.get('members', []),
					'rates': {},
				}
		db = new_db
		with open(DB_FILE, 'w', encoding='utf-8') as f:
			json.dump(db, f, ensure_ascii=False, indent=4)

	for k in ['secretary_code', 'judge_codes', 'judges_count', 'commands_num', 'commands']:
		if k not in db:
			if k == 'secretary_code':
				db[k] = generate_code()
			elif k in ('judge_codes', 'commands'):
				db[k] = {}
			else:
				db[k] = 0
	return db


def load_db():
	with db_lock:
		return _read_db_unlocked()


def update_db(mutator):
	with db_lock:
		db = _read_db_unlocked()
		mutator(db)
		with open(DB_FILE, 'w', encoding='utf-8') as f:
			json.dump(db, f, ensure_ascii=False, indent=4)
		return db


def get_local_ip():
	try:
		s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		s.connect(('8.8.8.8', 80))
		ip = s.getsockname()[0]
		s.close()
		return ip
	except Exception:
		return '127.0.0.1'


def generate_qr_code(data):
	qr = qrcode.make(data)
	buffer = BytesIO()
	qr.save(buffer, format='PNG')
	buffer.seek(0)
	return buffer


def get_base_url():
	port = 8501
	ip = get_local_ip()
	if ip == '127.0.0.1':
		try:
			return st.context.url.split('?')[0], False
		except Exception:
			return f'http://localhost:{port}', False
	return f'http://{ip}:{port}', True


with open('config.json', 'r', encoding='utf-8') as cfg:
	CRITERIA = json.load(cfg)['criteria']


# ==========================================================
#                    РОУТИНГ ПО РОЛЯМ
# ==========================================================

db = load_db()
code = st.query_params.get('code')

if not code:
	st.query_params['code'] = db['secretary_code']
	code = db['secretary_code']

if code == db['secretary_code']:
	ROLE = 'secretary'
	JUDGE_ID = None
elif code in db.get('judge_codes', {}):
	ROLE = 'judge'
	JUDGE_ID = db['judge_codes'][code]
else:
	st.error('❌ Недействительный код доступа. Обратитесь к секретарю.')
	st.stop()


# ==========================================================
#                       ПАНЕЛЬ СУДЬИ
# ==========================================================

def judge_panel(judge_id):
	st.set_page_config(page_title=f'Судья №{judge_id}', page_icon='⚖️')
	st.title(f'Панель судьи №{judge_id}')

	db = load_db()
	commands = db['commands']

	if not commands:
		st.warning('Секретарь ещё не добавил ни одной команды')
		st.stop()

	judge_key = str(judge_id)

	def format_cmd(k):
		rated = judge_key in commands[k].get('rates', {})
		return ('✅ ' if rated else '⬜ ') + commands[k]['title']

	if ('selected_command' not in st.session_state
			or st.session_state.selected_command not in commands):
		unrated = [k for k in commands if judge_key not in commands[k].get('rates', {})]
		st.session_state.selected_command = unrated[0] if unrated else list(commands.keys())[0]

	st.selectbox(
		'Выберите команду для оценки',
		options=list(commands.keys()),
		format_func=format_cmd,
		key='selected_command',
	)

	selected = st.session_state.selected_command
	cmd = commands[selected]

	st.divider()
	st.subheader(cmd['title'])

	with st.expander('📄 Описание и участники'):
		st.write('**Описание:**')
		st.write(cmd.get('description') or '—')
		st.write('**Участники:**')
		for m in cmd.get('members', []):
			st.markdown(f'- {m}')

	st.divider()

	rates = cmd.get('rates', {})
	already_rated = judge_key in rates

	if already_rated:
		saved = rates[judge_key]
		st.success(f'✅ Вы уже оценили эту команду. Ваш балл: **{sum(saved.values())}**')
		with st.expander('Показать мои оценки'):
			for crit_key, crit in CRITERIA.items():
				st.write(f"{crit['title']}: **{saved.get(crit_key, 0)}** / {crit['max']}")

		col1, col2 = st.columns(2)
		with col1:
			if st.button('🔄 Переоценить команду', use_container_width=True):
				for crit_key in CRITERIA:
					st.session_state[f'score_{selected}_{crit_key}_{judge_key}'] = \
						saved.get(crit_key, CRITERIA[crit_key]['default'])

				def _clear(d):
					d['commands'][selected]['rates'].pop(judge_key, None)

				update_db(_clear)
				st.rerun()
		with col2:
			if st.button('🧹 Сбросить все мои оценки', use_container_width=True):
				def _reset_all(d):
					for c in d['commands'].values():
						c.get('rates', {}).pop(judge_key, None)

				update_db(_reset_all)
				st.success('Все ваши оценки сброшены')
				st.rerun()
		return

	st.subheader('Выполните оценку')
	scores = {}
	for crit_key, crit in CRITERIA.items():
		widget_key = f'score_{selected}_{crit_key}_{judge_key}'
		value = st.slider(crit['title'], 0, crit['max'], crit['default'], key=widget_key)
		selected_range = '0'
		for rang in crit['ranges']:
			if value >= int(rang):
				selected_range = rang
		st.caption(crit['ranges'][selected_range])
		scores[crit_key] = value

	total = sum(scores.values())
	st.subheader(f'Итого: **{total}**')

	if st.button('📥 Зафиксировать оценку', type='primary', use_container_width=True):
		def _save(d):
			d['commands'][selected].setdefault('rates', {})[judge_key] = scores

		update_db(_save)
		st.success('Оценка сохранена!')
		st.rerun()

	st.divider()
	rated_count = sum(1 for c in commands.values() if judge_key in c.get('rates', {}))
	st.caption(f'Оценено команд: {rated_count} / {len(commands)}')


# ==========================================================
#                     ПАНЕЛЬ СЕКРЕТАРЯ
# ==========================================================

def render_commands_tab(db):
	commands = db['commands']
	st.subheader('Список команд')

	if not commands:
		st.warning('Пока не добавлено ни одной команды')
	else:
		for key, cmd in list(commands.items()):
			rated = len(cmd.get('rates', {}))
			with st.container(border=True):
				col1, col2 = st.columns([5, 1])
				with col1:
					st.markdown(f"**{cmd['title']}**")
					if cmd.get('description'):
						desc = cmd['description']
						st.caption(desc[:150] + ('...' if len(desc) > 150 else ''))
					st.caption(f"👥 {', '.join(cmd.get('members', [])) or '—'}")
					st.caption(f"✅ Оценок: {rated} / {db['judges_count']}")
				with col2:
					if st.button('🗑️', key=f'del_{key}', help='Удалить команду'):
						def _del(d):
							d['commands'].pop(key, None)

						update_db(_del)
						st.rerun()

	st.divider()

	with st.expander('➕ Добавить команду', expanded=not commands):
		with st.form('add_command', clear_on_submit=True):
			title = st.text_input('Название команды')
			description = st.text_area('Описание', height=100)
			members = st.text_input('Участники (через запятую)')
			submitted = st.form_submit_button('Добавить', type='primary')

		if submitted:
			if not title.strip():
				st.error('Название не может быть пустым')
			else:
				members_list = [m.strip() for m in re.split(r',\s*', members) if m.strip()]

				def _add(d):
					new_key = str(d['commands_num'])
					d['commands'][new_key] = {
						'title': title.strip(),
						'description': description.strip(),
						'members': members_list,
						'rates': {},
					}
					d['commands_num'] += 1

				update_db(_add)
				st.success(f'Команда "{title}" добавлена')
				st.rerun()

	st.divider()

	with st.expander('⚠️ Удаление'):
		st.warning('Удаление всех команд безвозвратно удалит все данные и все оценки.')
		confirm = st.text_input('Введите "DELETE" для подтверждения', key='confirm_delete_all')
		if st.button('🗑️ Удалить все команды', type='primary', disabled=confirm != 'DELETE'):
			def _reset(d):
				d['commands'] = {}
				d['commands_num'] = 0

			update_db(_reset)
			st.success('Все команды удалены')
			st.rerun()


def render_judges_tab(db):
	st.subheader('Настройка судей')
	current_count = db.get('judges_count', 0)

	col1, col2 = st.columns([2, 1])
	with col1:
		new_count = st.number_input(
			'Количество судей',
			min_value=1, max_value=50,
			value=max(current_count, 1),
			step=1,
		)
	with col2:
		st.write('')
		st.write('')
		regen = st.button('🔄 Сгенерировать QR-коды',
						  type='primary', use_container_width=True)

	if regen:
		def _gen(d):
			codes = {}
			for i in range(1, new_count + 1):
				codes[generate_code()] = i
			d['judge_codes'] = codes
			d['judges_count'] = new_count

		update_db(_gen)
		st.success(f'Сгенерировано {new_count} QR-кодов')
		st.rerun()

	judge_codes = db.get('judge_codes', {})
	if judge_codes:
		st.divider()
		st.subheader('QR-коды для судей')

		base_url, network_ok = get_base_url()
		if not network_ok:
			st.warning('Не удалось определить сетевой IP. Судьи смогут подключиться только с этого компьютера.')
		else:
			st.info('Судьи должны находиться в той же Wi-Fi сети, что и компьютер секретаря.')

		sorted_codes = sorted(judge_codes.items(), key=lambda x: x[1])
		cols_per_row = 3
		for i in range(0, len(sorted_codes), cols_per_row):
			cols = st.columns(cols_per_row)
			for j, (code, jid) in enumerate(sorted_codes[i:i + cols_per_row]):
				with cols[j]:
					url = f'{base_url}?code={code}'
					st.markdown(f'**Судья №{jid}**')
					st.image(generate_qr_code(url), use_container_width=True)
					with st.expander('Ссылка'):
						st.code(url, language='text')


# ==========================================================
#                       ИТОГИ / РЕЗУЛЬТАТЫ
# ==========================================================

def trim_scores(judge_scores):
	"""
	Правило снятия крайних: при 3+ оценках отбрасываются
	минимальная и максимальная оценки судей.

	Возвращает dict:
	  kept         – {judge_id: score} — оценки, участвующие в подсчёте
	  dropped_low  – (judge_id, score) | None — снятая минимальная
	  dropped_high – (judge_id, score) | None — снятая максимальная
	  was_trimmed  – bool
	"""
	if len(judge_scores) < 3:
		return {
			'kept': dict(judge_scores),
			'dropped_low': None,
			'dropped_high': None,
			'was_trimmed': False,
		}
	items = sorted(judge_scores.items(), key=lambda kv: kv[1])
	return {
		'kept': dict(items[1:-1]),
		'dropped_low': items[0],
		'dropped_high': items[-1],
		'was_trimmed': True,
	}


def compute_results(db):
	judges_count = db.get('judges_count', 0)
	commands = db.get('commands', {})

	if judges_count <= 0:
		max_jid = 0
		for cmd in commands.values():
			for jid_str in cmd.get('rates', {}):
				try:
					max_jid = max(max_jid, int(jid_str))
				except (ValueError, TypeError):
					pass
		judges_count = max_jid

	rows = []
	for key, cmd in commands.items():
		rates = cmd.get('rates', {})
		judge_scores = {}
		for jid_str, scores in rates.items():
			try:
				jid = int(jid_str)
			except (ValueError, TypeError):
				continue
			if 1 <= jid <= judges_count and isinstance(scores, dict):
				judge_scores[jid] = sum(scores.values())

		trim = trim_scores(judge_scores)
		kept_values = list(trim['kept'].values())
		avg = sum(kept_values) / len(kept_values) if kept_values else 0.0

		rows.append({
			'key': key,
			'title': cmd.get('title', f'Команда {key}'),
			'judge_scores': judge_scores,
			'kept': trim['kept'],
			'dropped_low': trim['dropped_low'],
			'dropped_high': trim['dropped_high'],
			'was_trimmed': trim['was_trimmed'],
			'avg': avg,
			'total': sum(kept_values),
			'rated_count': len(judge_scores),
		})

	rows.sort(key=lambda r: (r['avg'], r['total']), reverse=True)
	return rows, judges_count


def format_results_txt(db, rows, judges_count):
	lines = []
	lines.append('=' * 72)
	lines.append('ИТОГИ ОЦЕНКИ КОМАНД')
	lines.append(f'Сформировано: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
	lines.append(f'Судей в системе: {judges_count}')
	lines.append(f'Команд: {len(rows)}')
	lines.append('=' * 72)
	lines.append('')
	lines.append('ПРАВИЛО ПОДСЧЁТА:')
	lines.append('  При 3 и более оценках команды отбрасывается')
	lines.append('  максимальная и минимальная оценки судей,')
	lines.append('  итоговый балл считается по оставшимся.')
	lines.append('  При 1-2 оценках используются все имеющиеся.')
	lines.append('')
	lines.append('КРИТЕРИИ ОЦЕНКИ:')
	for k, c in CRITERIA.items():
		lines.append(f'  {k}. {c["title"]} (макс {c["max"]})')
	lines.append('')
	lines.append('-' * 72)
	lines.append('РЕЗУЛЬТАТЫ (по убыванию среднего балла):')
	lines.append('-' * 72)
	lines.append('')

	for place, row in enumerate(rows, 1):
		lines.append(f'МЕСТО {place}: {row["title"]}')
		lines.append(f'  Средний балл: {row["avg"]:.2f}')
		lines.append(f'  Сумма баллов: {row["total"]}')
		lines.append(f'  Оценено судей: {row["rated_count"]} / {judges_count}')

		if row['was_trimmed']:
			low_jid, low_val = row['dropped_low']
			high_jid, high_val = row['dropped_high']
			lines.append(
				f'  Правило снятия крайних: применено '
				f'(сняты Судья №{low_jid}={low_val} и Судья №{high_jid}={high_val})'
			)

		lines.append('  Оценки судей:')
		if row['judge_scores']:
			for jid in sorted(row['judge_scores']):
				score = row['judge_scores'][jid]
				mark = ''
				if row['was_trimmed']:
					if jid == row['dropped_low'][0]:
						mark = '  [СНЯТА — минимальная]'
					elif jid == row['dropped_high'][0]:
						mark = '  [СНЯТА — максимальная]'
				lines.append(f'    Судья №{jid}: {score}{mark}')
		else:
			lines.append('    (нет оценок)')

		cmd = db['commands'][row['key']]
		rates = cmd.get('rates', {})
		lines.append('  Средние по критериям (по всем оценкам):')
		for crit_key, crit in CRITERIA.items():
			values = [
				s[crit_key] for s in rates.values()
				if isinstance(s, dict) and crit_key in s
			]
			if values:
				lines.append(
					f'    {crit_key}. {crit["title"]}: '
					f'{sum(values) / len(values):.2f} (макс {crit["max"]})'
				)
			else:
				lines.append(f'    {crit_key}. {crit["title"]}: —')
		lines.append('')

	lines.append('=' * 72)
	lines.append('Конец отчёта')
	lines.append('=' * 72)
	return '\n'.join(lines)


def render_results_tab(db):
	st.header('Итоги оценки')

	commands = db.get('commands', {})
	if not commands:
		st.warning('Нет команд — подводить итоги пока нечего')
		return

	rows, judges_count = compute_results(db)
	rows_exist = any(r['rated_count'] > 0 for r in rows)

	if not rows_exist:
		st.info('Ни одна команда ещё не оценена судьями.')
	else:
		if any(r['was_trimmed'] for r in rows):
			st.info(
				'ℹ️ Применено правило снятия крайних: при 3+ оценках команды '
				'минимальная и максимальная оценки судей не учитываются.'
			)

		st.markdown('### Сводная таблица')
		table_data = []
		for place, row in enumerate(rows, 1):
			entry = {
				'Место': place,
				'Команда': row['title'],
				'Ср. балл': round(row['avg'], 2),
				'Сумма': row['total'],
				'Оценено': f"{row['rated_count']} / {judges_count}",
			}
			for jid in range(1, judges_count + 1):
				score = row['judge_scores'].get(jid)
				if score is None:
					entry[f'Судья {jid}'] = '—'
				elif row['was_trimmed'] and jid == row['dropped_low'][0]:
					entry[f'Судья {jid}'] = f'{score} ⬇'
				elif row['was_trimmed'] and jid == row['dropped_high'][0]:
					entry[f'Судья {jid}'] = f'{score} ⬆'
				else:
					entry[f'Судья {jid}'] = score
			table_data.append(entry)
		st.dataframe(table_data, use_container_width=True, hide_index=True)
		if any(r['was_trimmed'] for r in rows):
			st.caption('⬇ — снята как минимальная, ⬆ — снята как максимальная (в подсчёт не входят).')

		st.markdown('### Прогресс оценки')
		progress_data = []
		for key, cmd in commands.items():
			prow = {'Команда': cmd['title']}
			rates = cmd.get('rates', {})
			for jid in range(1, judges_count + 1):
				prow[f'Судья {jid}'] = '✅' if str(jid) in rates else '⬜'
			progress_data.append(prow)
		st.dataframe(progress_data, use_container_width=True, hide_index=True)

		with st.expander('🔍 Детально по критериям'):
			st.caption(
				'Средние по критериям считаются по всем оценкам (без снятия крайних); '
				'правило снятия применяется только к итоговому баллу команды.'
			)
			for row in rows:
				st.markdown(f'#### {row["title"]}')
				st.caption(
					f"Средний балл: **{row['avg']:.2f}** · "
					f"Сумма: **{row['total']}** · "
					f"Оценено: {row['rated_count']}/{judges_count}"
				)
				cmd = db['commands'][row['key']]
				rates = cmd.get('rates', {})
				crit_data = []
				for crit_key, crit in CRITERIA.items():
					values = [
						s[crit_key] for s in rates.values()
						if isinstance(s, dict) and crit_key in s
					]
					crit_data.append({
						'Критерий': crit['title'],
						'Макс': crit['max'],
						'Среднее': round(sum(values) / len(values), 2) if values else '—',
					})
				st.dataframe(crit_data, use_container_width=True, hide_index=True)
				st.divider()

	st.divider()
	st.subheader('📄 Сохранение итогов')
	txt_content = format_results_txt(db, rows, judges_count)

	col1, col2 = st.columns(2)
	with col1:
		if st.button('💾 Сохранить в results.txt', type='primary', use_container_width=True):
			try:
				with open(RESULTS_FILE, 'w', encoding='utf-8') as f:
					f.write(txt_content)
				st.success(f'Сохранено в `{os.path.abspath(RESULTS_FILE)}`')
			except Exception as e:
				st.error(f'Ошибка записи: {e}')
	with col2:
		st.download_button(
			'📥 Скачать results.txt',
			data=txt_content,
			file_name='results.txt',
			mime='text/plain',
			use_container_width=True,
		)

	with st.expander('👁 Предпросмотр файла'):
		st.code(txt_content, language='text')


def secretary_panel():
	st.set_page_config(page_title='Панель секретаря', page_icon='📋')
	st.title('Панель секретаря')

	db = load_db()
	tab_cmds, tab_judges, tab_results = st.tabs([
		'📝 Команды', '👥 Судьи и QR-коды', '📊 Итоги'
	])

	with tab_cmds:
		render_commands_tab(db)
	with tab_judges:
		render_judges_tab(db)
	with tab_results:
		render_results_tab(db)


# ==========================================================
#                       ЗАПУСК
# ==========================================================

if ROLE == 'secretary':
	secretary_panel()
elif ROLE == 'judge':
	judge_panel(JUDGE_ID)