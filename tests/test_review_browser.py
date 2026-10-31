"""Optional real-browser checks; set SO101_REVIEW_TEST_BUNDLE to a generated bundle."""
import json
import os
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
pytestmark = pytest.mark.browser


@pytest.fixture
def review_page():
    source = os.environ.get('SO101_REVIEW_TEST_BUNDLE')
    if not source:
        pytest.skip('Set SO101_REVIEW_TEST_BUNDLE to test an offline review with real videos')
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width':1600, 'height':1100}, accept_downloads=True, offline=True)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto((Path(source)/'index.html').as_uri())
        page.wait_for_function("document.querySelectorAll('video').length >= 2 && [...document.querySelectorAll('video')].every(v => v.readyState >= 2)")
        yield page
        assert not errors
        context.close(); browser.close()


def test_single_transport_plays_pauses_and_seeks_all_cameras(review_page):
    page = review_page
    page.locator('#play').click()
    page.wait_for_function("[...document.querySelectorAll('video')].every(v=>!v.paused && v.currentTime > 0)")
    page.wait_for_timeout(1300)
    clocks = page.evaluate("""() => { const d=JSON.parse(document.getElementById('data').textContent);return [...document.querySelectorAll('video')].map(v=>v.currentTime+d.videos[v.getAttribute('aria-label').replace('の同期映像','')].origin_s) }""")
    assert max(clocks)-min(clocks) < .09
    assert min(clocks) > .6
    page.locator('#play').click()
    assert page.locator('video').evaluate_all('(vs)=>vs.every(v=>v.paused)')
    page.locator('#slider').evaluate("el=>{el.value=5;el.dispatchEvent(new Event('input',{bubbles:true}))}")
    page.wait_for_function("[...document.querySelectorAll('video')].every(v=>!v.seeking)")
    deltas = page.evaluate("""() => { const d=JSON.parse(document.getElementById('data').textContent),r=d.rows.filter(r=>r.t<=5).at(-1);return [...document.querySelectorAll('video')].map(v=>Math.abs(v.currentTime-r.image_pts[v.getAttribute('aria-label').replace('の同期映像','')])) }""")
    assert max(deltas) < .025
    assert '5.000' in page.locator('#timeReadout').inner_text()
    page.locator('#speed').select_option('0.25')
    page.locator('#play').click()
    page.wait_for_timeout(700)
    assert page.locator('video').evaluate_all('(vs)=>vs.every(v=>!v.paused && v.playbackRate>=.24 && v.playbackRate<=.26)')
    page.locator('#play').click()


def test_3d_comparison_controls_and_lazy_episode_switch(review_page):
    page = review_page
    assert '近似モデル' in page.locator('#modelNotice').inner_text()
    assert page.locator('#eeUnavailable').is_hidden()
    box = page.locator('#eeCanvas').bounding_box()
    assert box['y']+box['height'] < page.locator('.transport').bounding_box()['y']
    page.locator('#overviewTab').click()
    count = page.locator('#cohortRows tr').count()
    assert count >= 2
    before = page.locator('#cohortCanvas').evaluate('c=>c.toDataURL()')
    box = page.locator('#cohortCanvas').bounding_box()
    page.mouse.move(box['x']+box['width']*.5,box['y']+box['height']*.5)
    page.mouse.down(); page.mouse.move(box['x']+box['width']*.5+90,box['y']+box['height']*.5+25,steps=8); page.mouse.up()
    after = page.locator('#cohortCanvas').evaluate('c=>c.toDataURL()')
    assert before != after
    page.locator('#alignStarts').check()
    page.locator('#cohortSort').select_option('startpoint')
    page.locator('#includeRejected').check()
    assert page.locator('#cohortRows tr').count() >= count
    page.locator('#cohortRows button.link-button').nth(1).click()
    assert page.locator('#inspectView').is_visible()
    page.wait_for_function("[...document.querySelectorAll('video')].every(v=>v.readyState>=2)")
    page.locator('#signalKind').select_option('latency')
    assert 'Recorder' in page.locator('#signalCaption').inner_text()


def test_notes_ranges_markers_and_json_csv_roundtrip(review_page, tmp_path):
    page = review_page
    page.locator('#reviewNote').fill('配置戻しを確認。<script>は文字として保存。')
    page.locator('[data-decision=needs_review]').click()
    page.locator('#slider').evaluate("el=>{el.value=2;el.dispatchEvent(new Event('input',{bubbles:true}))}")
    page.locator('#setIn').click()
    page.locator('#addBookmark').click()
    page.locator('#slider').evaluate("el=>{el.value=8;el.dispatchEvent(new Event('input',{bubbles:true}))}")
    page.locator('#setOut').click()
    with page.expect_download() as downloaded:
        page.locator('#exportReview').click()
    path=tmp_path/'review.json';downloaded.value.save_as(path)
    result=json.loads(path.read_text())
    assert result['format']=='so101_review_annotations_v1'
    first=result['episodes'][0]
    assert first['range']=={'start_s':2,'end_s':8}
    assert first['bookmarks'][0]['t']==2
    assert first['decision']=='needs_review'
    first['decision']='accepted'; first['note']='読み込み確認'
    page.locator('#importReview').set_input_files({'name':'review.json','mimeType':'application/json','buffer':json.dumps(result).encode()})
    assert page.locator('#reviewNote').input_value()=='読み込み確認'
    assert page.locator('#reviewState').inner_text()=='確認済み'
    with page.expect_download() as downloaded:
        page.locator('#exportCsv').click()
    path=tmp_path/'summary.csv';downloaded.value.save_as(path)
    assert 'startpoint_distance_m' in path.read_text(encoding='utf-8-sig').splitlines()[0]
    page.reload()
    page.wait_for_function("document.querySelectorAll('video').length>=2")
    assert page.locator('#reviewNote').input_value()=='読み込み確認'


def test_range_loop_and_keyboard_pause_all_cameras(review_page):
    page = review_page
    page.locator('#slider').evaluate("el=>{el.value=.5;el.dispatchEvent(new Event('input',{bubbles:true}))}")
    page.locator('#setIn').click()
    page.locator('#slider').evaluate("el=>{el.value=1.5;el.dispatchEvent(new Event('input',{bubbles:true}))}")
    page.locator('#setOut').click()
    page.locator('#loopRange').check()
    page.locator('#play').click()
    page.wait_for_timeout(2600)
    assert .5 <= float(page.locator('#slider').input_value()) <= 1.5
    assert page.locator('video').evaluate_all('(vs)=>vs.every(v=>!v.paused)')
    page.locator('#episodeTitle').click()
    page.keyboard.press('Space')
    assert page.locator('video').evaluate_all('(vs)=>vs.every(v=>v.paused)')
