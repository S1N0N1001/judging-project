"""Web UI. Start using launcher.py (or Judging.exe in the release)."""
import json
import os
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, urlsplit

import qrcode
import streamlit as st

from judging_core import ALGORITHMS, calculate, export_files
from judging_store import Store
from judging_network import local_addresses, public_url, validate_manual_url

st.set_page_config(page_title='Судейство', page_icon='⚖️', layout='wide')
store = Store()
STATUSES = {'preparation': 'Подготовка', 'scoring': 'Оценивание', 'closed': 'Закрыто', 'approved': 'Утверждено'}


def run_action(fn, *args, **kwargs):
    try:
        result = fn(*args, **kwargs)
        st.session_state['notice'] = 'Действие выполнено'
        return result
    except (ValueError, PermissionError) as exc:
        st.error(str(exc))
        return False


def qr(data):
    b = BytesIO()
    qrcode.make(data).save(b, format='PNG')
    return b.getvalue()


def fresh():
    st.rerun()


def initial_setup():
    st.title('Первый запуск')
    expected = os.environ.get('JUDGING_SETUP_TOKEN', '')
    provided = st.query_params.get('setup', '')
    # Secret is only supplied by the local launcher; no IP/Host-header trust.
    if not expected or provided != expected:
        st.info('Для первоначальной настройки откройте приложение кнопкой «Открыть интерфейс» в локальном лаунчере.')
        st.stop()
    with st.form('setup'):
        password = st.text_input('Пароль секретаря (минимум 12 символов)', type='password')
        repeat = st.text_input('Повторите пароль', type='password')
        submitted = st.form_submit_button('Создать доступ')
    if submitted:
        if password != repeat:
            st.error('Пароли не совпадают')
        else:
            criteria = json.loads((Path(__file__).parent / 'config.json').read_text(encoding='utf-8'))['criteria']
            if run_action(store.setup, password, provided, expected, criteria) is not False:
                st.query_params.clear()
                fresh()
    st.stop()


def login():
    st.title('Вход секретаря')
    st.caption('Судьи открывают индивидуальную ссылку, выданную секретарём.')
    with st.form('login'):
        password = st.text_input('Пароль', type='password')
        submitted = st.form_submit_button('Войти')
    if submitted:
        token = run_action(store.login, password)
        if token:
            st.session_state['admin_token'] = token
            st.query_params.clear()
            fresh()
    st.stop()


@st.fragment(run_every=3)
def judge_status(token):
    try:
        view = store.judge_view(token)
        event = view['event']
        title = next((p['title'] for p in view['participants'] if p['id'] == event['current_id']), 'не назначена')
        st.info(f"{STATUSES[event['status']]} · Текущая работа: {title}")
    except PermissionError as exc:
        st.error(str(exc))


def judge_panel(token):
    view = store.judge_view(token)
    event, judge = view['event'], view['judge']
    st.title(f"Судья №{judge['number']}")
    judge_status(token)
    if st.button('Обновить список работ'):
        fresh()
    participants = view['participants']
    if not participants:
        st.info('Секретарь ещё не открыл работы для оценивания.')
        return
    by_id = {p['id']: p for p in participants}
    default = list(by_id).index(event['current_id']) if event['current_id'] in by_id else 0
    pid = st.selectbox('Работа', list(by_id), index=default, format_func=lambda p: f"{by_id[p]['number']}. {by_id[p]['title']}", key='selected_work')
    p = by_id[pid]
    st.subheader(p['title'])
    st.write(p['description'])
    st.caption('Участники: ' + ', '.join(p['members']))
    st.caption(f"Тип: {p['work_type'] or 'не указан'} · Стадия: {p['stage'] or 'не указана'}")
    if p['materials']:
        st.text('Материалы: ' + p['materials'])
    card = next((c for c in view['cards'] if c['participant_id'] == pid), None)
    locked = event['status'] != 'scoring' or bool(card and card['status'] == 'submitted')
    if card:
        st.caption(f"Сохранено: {card['updated_at']} · Версия {card['version']}")
    if locked:
        st.success('Карточка отправлена. Изменение доступно после разблокировки секретарём.' if card and card['status'] == 'submitted' else 'Оценивание закрыто.')
        if card:
            for k, c in event['criteria'].items():
                st.write(f"{c['title']}: {card['scores'].get(k, 'не оценено')} / {c['max']}")
        return
    # Keep the version loaded with the input, not a newer DB version from a rerun.
    context = f"{judge['id']}_{pid}"
    version_key = f'version_{context}'
    if version_key not in st.session_state:
        st.session_state[version_key] = card['version'] if card else 0
    st.caption('Нажмите поле критерия и выберите балл из списка. Счётчик заполнения обновляется после выбора.')
    scores = {}
    for k, c in event['criteria'].items():
        saved_value = card['scores'].get(k) if card else None
        value = st.selectbox(c['title'], options=list(range(c['max'] + 1)), index=saved_value, key=f'score_{context}_{k}', placeholder='Выберите балл', width='stretch')
        if value is not None:
            scores[k] = value
            band = max(int(b) for b in c['ranges'] if int(b) <= value)
            st.caption(c['ranges'][str(band)])
        with st.expander('Все уровни: ' + c['title']):
            bounds = sorted(map(int, c['ranges']))
            for idx, lower in enumerate(bounds):
                upper = bounds[idx + 1] - 1 if idx + 1 < len(bounds) else c['max']
                st.write(f"{lower}–{upper}: {c['ranges'][str(lower)]}")
    comment = st.text_area('Комментарий к работе (необязательно)', value=card['comment'] if card else '', key=f'comment_{context}')
    st.write(f'Заполнено: {len(scores)} / 7 · Сумма выбранных баллов: {sum(scores.values())}')
    st.caption('Изменения на экране ещё не сохранены. Нажмите «Сохранить черновик» или «Отправить». При потере связи не закрывайте вкладку.')
    a, b = st.columns(2)
    save = a.button('Сохранить черновик', width='stretch')
    submit = b.button('Отправить и заблокировать', type='primary', disabled=len(scores) != 7, width='stretch')
    if save or submit:
        version = run_action(store.save_card, token, pid, scores, comment, st.session_state[version_key], submit=submit)
        if version is not False:
            st.session_state[version_key] = version
            fresh()
    if st.button('Загрузить сохранённую версию (заменить ввод)'):
        for key in list(st.session_state):
            if context in key:
                del st.session_state[key]
        fresh()
    st.caption(f"Отправлено работ: {sum(c['status'] == 'submitted' for c in view['cards'])} / {len(participants)}")


def settings_tab(token, view):
    event = view['event']
    if event['status'] == 'preparation':
        config_upload = st.file_uploader('Обновить критерии из config.json (до открытия)', type=['json'], key='criteria_upload')
        criteria = event['criteria']
        if config_upload:
            try:
                from judging_core import validate_config
                criteria = json.loads(config_upload.getvalue().decode('utf-8-sig'))['criteria']
                validate_config(criteria)
            except (ValueError, KeyError, TypeError) as exc:
                st.error('Конфигурация не принята: ' + str(exc))
                return
        with st.form('event_settings'):
            name = st.text_input('Название мероприятия', value=event['name'])
            algorithm = st.selectbox('Алгоритм расчёта', list(ALGORITHMS), index=list(ALGORITHMS).index(event['algorithm']), format_func=ALGORITHMS.get)
            enable_threshold = st.checkbox('Использовать порог расхождения', value=event['threshold'] is not None)
            threshold = st.number_input('Порог: доля максимума критерия', min_value=0.0, max_value=1.0, value=event['threshold'] if event['threshold'] is not None else 0.5, step=0.05)
            confirmed = st.checkbox('Подтверждаю алгоритм, итоговое округление half-up до 0,01 и общие места 1, 2, 2, 4')
            st.caption('Штрафы и специальные максимумы по типам работ отключены до утверждения соответствующих шкал.')
            submitted = st.form_submit_button('Сохранить и подтвердить методику')
        if submitted:
            if not confirmed:
                st.error('Подтвердите правила перед открытием мероприятия')
            elif run_action(store.configure, token, name, algorithm, threshold if enable_threshold else None, criteria) is not False:
                fresh()
        with st.expander('Импорт прежнего db.json (только в пустое мероприятие)'):
            st.warning('Старые ссылки будут заменены. Исходный JSON останется без изменений; до импорта создаётся копия. После импорта подтвердите настройки методики.')
            uploaded = st.file_uploader('Выберите db.json', type=['json'])
            if uploaded and st.button('Импортировать данные'):
                path = store.root / 'data' / 'import.json'
                path.write_bytes(uploaded.getvalue())
                if run_action(store.migrate, token, path) is not False:
                    fresh()
    st.write('Алгоритм: ' + ALGORITHMS[event['algorithm']])
    reason = st.text_input('Причина возобновления / разблокировки', key='event_reason')
    targets = {'preparation': [('scoring', 'Открыть оценивание')], 'scoring': [('closed', 'Закрыть оценивание')], 'closed': [('approved', 'Утвердить протокол'), ('scoring', 'Возобновить оценивание')], 'approved': [('scoring', 'Возобновить с сохранением утверждённого снимка')]}
    for target, label in targets[event['status']]:
        if st.button(label):
            if run_action(store.transition, token, target, reason) is not False:
                fresh()
    if st.button('Создать резервную копию'):
        result = run_action(store.backup, token)
        if result:
            st.success(f'Резервная копия: {result}')
    st.caption('Каталог данных: ' + str(store.root))


def participants_tab(token, view):
    preparing = view['event']['status'] == 'preparation'
    if preparing:
        with st.form('participant', clear_on_submit=True):
            title = st.text_input('Название команды / работы')
            description = st.text_area('Аннотация')
            members = st.text_input('Участники через запятую')
            work_type = st.text_input('Тип / категория работы')
            stage = st.text_input('Стадия реализации')
            materials = st.text_input('Материалы (адрес или путь)')
            add = st.form_submit_button('Добавить работу')
        if add and run_action(store.add_participant, token, title, description, [s.strip() for s in members.split(',') if s.strip()], work_type, stage, materials) is not False:
            fresh()
    for p in view['participants']:
        if p['archived']:
            continue
        with st.container(border=True):
            st.write(f"**{p['number']}. {p['title']}**")
            st.caption(p['description'])
            if view['event']['status'] == 'scoring' and st.button('Открыть судьям / сделать текущей', key='open_' + p['id']):
                if run_action(store.open_participant, token, p['id']) is not False:
                    fresh()
            if preparing:
                with st.expander('Редактировать работу'):
                    with st.form('edit_' + p['id']):
                        title = st.text_input('Название', value=p['title'])
                        desc = st.text_area('Описание', value=p['description'])
                        members = st.text_input('Участники через запятую', value=', '.join(p['members']))
                        typ = st.text_input('Тип', value=p['work_type'])
                        stage = st.text_input('Стадия', value=p['stage'])
                        materials = st.text_input('Материалы', value=p['materials'])
                        edit = st.form_submit_button('Сохранить работу')
                    if edit and run_action(store.edit_participant, token, p['id'], title, desc, [s.strip() for s in members.split(',') if s.strip()], typ, stage, materials) is not False:
                        fresh()
                with st.expander('Архивирование'):
                    reason = st.text_input('Причина', key='archive_reason_' + p['id'])
                    if st.button('Архивировать', key='archive_' + p['id']):
                        if run_action(store.archive_participant, token, p['id'], reason) is not False:
                            fresh()


@st.cache_data(ttl=5, show_spinner=False)
def detected_addresses():
    return local_addresses()


def judges_tab(token, view):
    if st.button('Обновить сетевые адреса'):
        detected_addresses.clear()
        fresh()
    addresses = detected_addresses()
    labels = {a['ip']: f"{a['ip']} — {a['name']}" for a in addresses}
    options = list(labels) + ['manual']
    if st.session_state.get('lan_adapter') not in options:
        st.session_state['lan_adapter'] = options[0]
    selected = st.selectbox('Сетевой адрес компьютера для судей', options, format_func=lambda ip: labels.get(ip, 'Указать адрес вручную'), key='lan_adapter')
    try:
        port = int(os.environ.get('JUDGING_PORT') or urlsplit(st.context.url).port or 8501)
        if selected == 'manual':
            entered = st.text_input('Адрес сервера для телефонов (LAN-IP и фактический порт)', value=os.environ.get('JUDGING_PUBLIC_URL', ''), placeholder='http://172.17.18.194:8501')
            base = validate_manual_url(entered) if entered else None
        else:
            base = public_url(selected, port)
            st.code(base, language='text')
    except ValueError as exc:
        st.error(str(exc))
        base = None
    st.caption('Выберите адаптер сети, к которой подключены телефоны. При смене DHCP-адреса обновите адреса и выдайте обновлённый QR; код судьи менять не нужно.')
    if len(addresses) > 1:
        st.info('Найдено несколько адаптеров. Выберите Ethernet или Wi-Fi вашей локальной сети; адрес VPN или виртуальной машины обычно не подходит для телефонов.')
    if not addresses:
        st.warning('LAN IPv4 не найден. Проверьте подключение компьютера и укажите его IPv4 вручную.')
    if os.environ.get('JUDGING_BIND_ADDRESS') == '127.0.0.1':
        st.error('Сервер запущен только для этого компьютера. Перезапустите лаунчер без --address 127.0.0.1 для доступа с телефонов.')
    if view['event']['status'] == 'preparation' and st.button('Добавить судью'):
        raw = run_action(store.add_judge, token)
        if raw:
            st.session_state['new_judge_link'] = raw
            fresh()
    if st.session_state.get('new_judge_link') and base:
        link = base.rstrip('/') + '/?code=' + quote(st.session_state['new_judge_link'])
        st.success('Сохраните ссылку: она показывается только в текущей сессии. Позже доступ можно перевыпустить.')
        st.code(link)
        st.image(qr(link), width=220)
        if st.button('Скрыть выданную ссылку'):
            del st.session_state['new_judge_link']
            fresh()
    for judge in view['judges']:
        with st.expander(f"Судья №{judge['number']}"):
            reason = st.text_input('Причина перевыпуска или удаления', key='rotate_reason_' + judge['id'])
            if st.button('Перевыпустить доступ только этому судье', key='rotate_' + judge['id']):
                raw = run_action(store.rotate_judge, token, judge['id'], reason)
                if raw:
                    st.session_state['new_judge_link'] = raw
                    fresh()
            if view['event']['status'] == 'preparation':
                st.caption('Удаление отзовёт ссылку и исключит судью из состава. История и прежние записи сохраняются.')
                confirmed = st.checkbox('Подтверждаю удаление судьи', key='remove_confirm_' + judge['id'])
                if st.button('Удалить судью', key='remove_' + judge['id'], disabled=not confirmed):
                    if run_action(store.remove_judge, token, judge['id'], reason) is not False:
                        st.session_state.pop('new_judge_link', None)
                        fresh()
            else:
                st.caption('Удаление судьи доступно до открытия оценивания; состав уже закреплён.')
    with st.expander('QR подключения к Wi-Fi'):
        ssid = st.text_input('Имя сети')
        password = st.text_input('Пароль Wi-Fi', type='password')
        if ssid and password:
            def esc(s):
                return ''.join('\\' + c if c in '\\;,:"' else c for c in s)
            st.image(qr(f'WIFI:T:WPA;S:{esc(ssid)};P:{esc(password)};;'), width=220)


def progress_tab(token, view):
    st.caption('Числовые оценки доступны только после закрытия. Эта таблица обновляется кнопкой.')
    if st.button('Обновить статусы'):
        fresh()
    rows = []
    cards = {(c['participant_id'], c['judge_id']): c for c in view['cards']}
    for p in view['participants']:
        if p['archived']:
            continue
        row = {'Работа': p['title']}
        for j in view['judges']:
            c = cards.get((p['id'], j['id']))
            row[f"Судья {j['number']}"] = {'draft': 'Черновик', 'submitted': 'Отправлена'}.get(c['status'], 'Нет') if c else 'Нет'
        rows.append(row)
    st.dataframe(rows, hide_index=True, width='stretch')
    if view['event']['status'] == 'scoring' and view['cards']:
        with st.expander('Разблокировать карточку'):
            eligible = [c for c in view['cards'] if c['status'] == 'submitted']
            if eligible:
                pmap = {p['id']: p['title'] for p in view['participants']}
                jmap = {j['id']: j['number'] for j in view['judges']}
                idx = st.selectbox('Карточка', range(len(eligible)), format_func=lambda i: f"{pmap[eligible[i]['participant_id']]} — судья {jmap[eligible[i]['judge_id']]}")
                reason = st.text_input('Обязательная причина', key='unlock_reason')
                if st.button('Разблокировать без удаления баллов'):
                    c = eligible[idx]
                    if run_action(store.unlock, token, c['judge_id'], c['participant_id'], reason) is not False:
                        fresh()


def results_tab(token, event):
    if event['status'] not in ('closed', 'approved'):
        st.info('Закройте оценивание, чтобы просмотреть числовые результаты и экспортировать протокол.')
        return
    # Keep a single immutable view across download button reruns.
    cache_key = event['status'] + event['updated_at']
    if st.session_state.get('export_key') != cache_key:
        st.session_state['export_snapshot'] = store.snapshot(token)
        st.session_state['export_key'] = cache_key
    snapshot = st.session_state['export_snapshot']
    results = calculate(snapshot)
    search = st.text_input('Поиск команды / работы')
    only_complete = st.checkbox('Только полностью оценённые', value=False)
    rows = [{'Место': r['rank'], 'Команда / работа': r['participant']['title'], 'Итог': float(r['total']) if r['total'] is not None else None, 'Отправлено': r['count'], 'Ожидается': r['expected']} for r in results if search.casefold() in r['participant']['title'].casefold() and (r['complete'] or not only_complete)]
    st.dataframe(rows, hide_index=True, width='stretch')
    st.caption('Выгрузка содержит все работы, независимо от фильтра. Снимок: ' + snapshot['id'])
    universal = st.checkbox('Универсальный CSV: разделитель запятая, десятичная точка')
    files = export_files(snapshot, universal)
    labels = {'summary.csv': 'Скачать сводку CSV', 'scores.csv': 'Скачать оценки судей CSV', 'criteria.csv': 'Скачать расчёт по критериям CSV', 'protocol.zip': 'Скачать полный протокол ZIP'}
    stamp = snapshot['created_at'].replace(':', '').replace('-', '').replace('+', '_')
    for name, label in labels.items():
        st.download_button(label, files[name], file_name=f"results_{event['id']}_{stamp}_{name}", mime='application/zip' if name.endswith('zip') else 'text/csv')
    flagged = [(r['participant']['title'], k) for r in results for k in event['criteria'] if any(d['criterion'] == k and d['discussion'] for d in r['detail'])]
    if flagged:
        st.warning('Порог расхождения превышен. До утверждения разберите отмеченные критерии.')
        st.dataframe([{'Работа': p, 'Критерий': event['criteria'][k]['title']} for p, k in flagged], hide_index=True)


def admin_panel(token):
    view = store.admin_view(token)
    event = view['event']
    st.title(event['name'])
    st.caption('Состояние: ' + STATUSES[event['status']])
    if st.sidebar.button('Выйти'):
        store.logout(token)
        st.session_state.clear()
        fresh()
    tabs = st.tabs(['Настройки', 'Работы', 'Судьи и QR', 'Прогресс', 'Итоги и CSV', 'Журнал'])
    with tabs[0]:
        settings_tab(token, view)
    with tabs[1]:
        participants_tab(token, view)
    with tabs[2]:
        judges_tab(token, view)
    with tabs[3]:
        progress_tab(token, view)
    with tabs[4]:
        results_tab(token, event)
    with tabs[5]:
        st.dataframe(store.audit(token), hide_index=True, width='stretch')


try:
    if not store.initialized():
        initial_setup()
    if 'notice' in st.session_state:
        st.success(st.session_state.pop('notice'))
    code = st.query_params.get('code')
    if code:
        judge_panel(code)
    else:
        token = st.session_state.get('admin_token')
        if not token:
            login()
        admin_panel(token)
except (ValueError, PermissionError) as exc:
    st.error(str(exc))
    if st.button('Вернуться ко входу'):
        st.query_params.clear()
        st.session_state.clear()
        fresh()
